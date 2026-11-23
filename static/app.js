const API_TOKEN = window.LOCAL_APP_TOKEN;
const API_ROOT = "/api";
const PROVIDER_NAMES = { edge: "Edge‑TTS", elevenlabs: "ElevenLabs", cartesia: "Cartesia", openai: "OpenAI TTS", gemini: "Google Gemini TTS" };
const state = {
  project: null,
  scenes: [],
  notes: [],
  costs: { image_cost: 0, voice_cost: 0, llm_tokens: 0 },
  soundEffects: [],
  assets: [],
  styles: [],
  style: null,
  voiceBusy: false,
  sceneVoiceChoices: new Map(),
  selectedSceneId: null,
  currentTime: 0,
  playing: false,
  providers: [],
  providerModels: JSON.parse(localStorage.getItem("studio-provider-models") || "{}"),
  timelineZoom: 100,
  timer: null,
  currentView: "timeline",
};

const providerPresentation = {
  openai: { group: "llm", mark: "O", tone: "green" },
  anthropic: { group: "llm", mark: "A", tone: "purple" },
  gemini: { group: "llm", mark: "G", tone: "green" },
  elevenlabs: { group: "media", mark: "11", tone: "purple" },
  cartesia: { group: "media", mark: "C", tone: "green" },
  stability: { group: "media", mark: "S", tone: "purple" },
  replicate: { group: "media", mark: "R", tone: "green" },
  jamendo: { group: "audio", mark: "J", tone: "green" },
  freesound: { group: "audio", mark: "F", tone: "purple" },
};

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) headers.set("X-Local-App-Token", API_TOKEN);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_ROOT}${path}`, { ...options, method, headers });
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text || response.statusText }; }
  if (!response.ok) {
    const detail = Array.isArray(payload.detail) ? payload.detail.map((entry) => entry.msg).join(". ") : payload.detail;
    throw new Error(detail || `Error HTTP ${response.status}`);
  }
  return payload;
}

function toast(message, kind = "info") {
  const element = document.getElementById("toast");
  element.textContent = message;
  element.className = `toast visible ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("visible"), 3400);
}

function formatTime(seconds, decimals = 0) {
  const safe = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  const sec = decimals ? remainder.toFixed(decimals).padStart(decimals + 3, "0") : String(Math.floor(remainder)).padStart(2, "0");
  return `${String(minutes).padStart(2, "0")}:${sec}`;
}

function formatHeaderDuration(seconds) {
  const total = Math.floor(Number(seconds) || 0);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function selectedScene() {
  return state.scenes.find((scene) => scene.id === state.selectedSceneId) || state.scenes[0] || null;
}

function getSceneAtTime(seconds) {
  return state.scenes.find((scene) => seconds >= scene.start_seconds && seconds < scene.end_seconds) || state.scenes[state.scenes.length - 1];
}

function setView(viewName) {
  const active = document.getElementById("timeline-audio");
  if (viewName !== "audio") document.getElementById("gemini-sample-player").pause();
  if (viewName !== "timeline") {
    active.pause();
    clearInterval(state.timer);
    state.playing = false;
    document.getElementById("play-button").textContent = "▶";
  }
  if (viewName !== "review") document.getElementById("review-video").pause();
  state.currentView = viewName;
  const editor = document.getElementById("editor-view");
  const script = document.getElementById("script-view");
  const settings = document.getElementById("settings-view");
  const review = document.getElementById("review-view");
  const audio = document.getElementById("audio-view");
  const exportView = document.getElementById("export-view");
  const workspace = document.getElementById("workspace-view");
  const production = ["prompts", "resources", "scenes", "styles"];
  [editor, script, settings, review, audio, exportView, workspace,
    ...production.map((name) => document.getElementById(`${name}-view`))].forEach((element) => element.classList.add("hidden"));
  if (viewName === "timeline") editor.classList.remove("hidden");
  else if (viewName === "script") script.classList.remove("hidden");
  else if (viewName === "settings") settings.classList.remove("hidden");
  else if (viewName === "review") review.classList.remove("hidden");
  else if (viewName === "audio") { audio.classList.remove("hidden"); renderMediaPanels(); }
  else if (viewName === "export") { exportView.classList.remove("hidden"); renderMediaPanels(); }
  else if (production.includes(viewName)) {
    document.getElementById(`${viewName}-view`).classList.remove("hidden");
    renderProductionView();
  }
  else {
    workspace.classList.remove("hidden");
    renderWorkspace();
  }
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === viewName));
  if (viewName === "settings") loadProviders();
  if (viewName === "review") renderReview();
  if (viewName === "script") syncScriptProvider();
  if (viewName === "timeline") renderTimeline();
}

function renderWorkspace() {
  if (!state.project) return;
  const view = state.currentView;
  const titles = { home: "Inicio", projects: "Proyecto local", analysis: "Datos de las escenas", help: "Flujo de producción" };
  document.getElementById("workspace-title").textContent = titles[view] || "Proyecto local";
  const content = document.getElementById("workspace-content");
  if (view === "analysis") {
    content.innerHTML = state.scenes.map((scene) => `
      <div class="sfx-row"><strong>Escena ${scene.order_index}</strong>
      <span>${[scene.subject, scene.place, scene.era, ...(scene.keywords || [])].filter(Boolean).map(escapeHTML).join(" · ") || "Sin datos de análisis"}</span></div>`).join("");
  } else if (view === "help") {
    content.innerHTML = "<ol><li>Pega el guion en bloques de escena.</li><li>Copia los prompts con tu estilo y genera las imágenes en tu herramienta elegida.</li><li>Sube las imágenes a Recursos y asígnalas.</li><li>Genera las voces pendientes en la línea de tiempo y exporta el MP4.</li></ol>";
  } else {
    const pendingImages = state.scenes.filter((scene) => !scene.image_url).length;
    const pendingVoices = state.scenes.filter((scene) => !scene.audio_url).length;
    content.innerHTML = `<h2>${escapeHTML(state.project.title)}</h2><p>${state.scenes.length} escenas · ${formatHeaderDuration(state.project.duration_seconds)}</p>
      <p class="media-hint">${pendingImages} escenas sin imagen · ${pendingVoices} sin voz individual</p>`;
  }
}

function renderHeader() {
  const title = state.project?.title || "Proyecto sin título";
  document.getElementById("top-project-title").textContent = title;
  document.getElementById("sidebar-project-title").textContent = title;
  document.getElementById("metric-scenes").textContent = state.scenes.length;
  document.getElementById("resource-count").textContent = state.assets.length;
  const assignedImages = state.scenes.filter((scene) => scene.image_path).length;
  document.getElementById("metric-images").textContent = `${assignedImages}/${state.scenes.length}`;
  document.getElementById("metric-duration").textContent = formatHeaderDuration(state.project?.duration_seconds || 0);
  document.getElementById("metric-tokens").textContent = `${Number(state.costs?.llm_tokens || 0).toLocaleString("es-CO")} tok`;
  document.getElementById("metric-cost").textContent = `$${Number(state.costs?.total_cost || 0).toFixed(2)}`;
  document.getElementById("total-time").textContent = formatTime(state.project?.duration_seconds || 0, 2);
  document.getElementById("note-nav-count").textContent = state.notes.filter((note) => note.status === "pendiente").length;
  document.getElementById("review-count-number").textContent = state.notes.filter((note) => note.status === "pendiente").length;
  document.getElementById("connected-summary").textContent = `${state.providers.filter((provider) => provider.configured).length} servicios conectados`;
  document.getElementById("settings-status-dot").classList.toggle("visible", state.providers.some((provider) => provider.configured));
}

function renderInspector() {
  const scene = selectedScene();
  if (!scene) return;
  const order = String(scene.order_index).padStart(2, "0");
  const duration = Math.max(0, scene.end_seconds - scene.start_seconds);
  document.getElementById("inspector-scene-label").textContent = `ESCENA ${order}`;
  document.getElementById("stage-scene-label").textContent = `ESCENA ${order}`;
  document.getElementById("stage-scene-index").textContent = `#${String(scene.order_index).padStart(3, "0")}`;
  document.getElementById("inspector-match").textContent = `${scene.match_score}%`;
  document.getElementById("stage-match").textContent = `${scene.match_score}%`;
  document.getElementById("inspector-time-range").textContent = `${formatTime(scene.start_seconds, 2)} → ${formatTime(scene.end_seconds, 2)}`;
  document.getElementById("inspector-duration").textContent = `${duration.toFixed(2)}s`;
  document.getElementById("narration-input").value = scene.narration;
  document.getElementById("visual-prompt").value = scene.visual_prompt || "";
  document.getElementById("meta-subject").textContent = scene.subject || "Por definir";
  document.getElementById("meta-place").textContent = scene.place || "Por definir";
  document.getElementById("meta-era").textContent = scene.era || "Por definir";
  document.getElementById("meta-keywords").innerHTML = (scene.keywords || []).map((word) => `<span class="keyword">${escapeHTML(word)}</span>`).join("");
  document.getElementById("lock-scene").innerHTML = scene.locked ? "♙ &nbsp; Desbloquear" : "♙ &nbsp; Bloquear";
  document.getElementById("asset-name").textContent = scene.image_path ? `Escena ${String(scene.order_index).padStart(3, "0")}` : "Ilustración de escena";
  document.getElementById("asset-caption").textContent = scene.image_path ? "Imagen local · asignada" : "Vista previa de estilo · ejemplo";
  const stage = document.getElementById("video-stage");
  const frame = stage.querySelector(".stage-art-frame");
  let uploaded = frame.querySelector(".uploaded-scene-image");
  if (scene.image_url) {
    if (!uploaded) {
      uploaded = document.createElement("img");
      uploaded.className = "uploaded-scene-image";
      frame.append(uploaded);
    }
    uploaded.src = scene.image_url;
    uploaded.hidden = false;
    frame.querySelector(".story-illustration").hidden = true;
  } else {
    if (uploaded) uploaded.hidden = true;
    frame.querySelector(".story-illustration").hidden = false;
  }
  renderTimeline();
  renderLibrary();
}

function escapeHTML(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

async function copyText(text, message) {
  if (!text.trim()) return toast("Todavía no hay texto para copiar.");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const field = document.createElement("textarea");
    field.value = text;
    field.className = "clipboard-field";
    document.body.append(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    if (!copied) throw new Error("El portapapeles no está disponible.");
  }
  toast(message, "success");
}

function styledPrompt(scene) {
  const draft = document.querySelector(`[data-edit-prompt="${scene.id}"]`);
  const prompt = (draft?.value ?? scene.visual_prompt).trim();
  return prompt ? [prompt, state.style?.style_prompt.trim()].filter(Boolean).join("\n\n") : "";
}

function geminiProvider() {
  return state.providers.find((provider) => provider.id === "gemini");
}

function geminiVoiceOptions(selected) {
  return Object.entries(geminiProvider()?.speech?.voices || {}).map(([voice, description]) =>
    `<option value="${escapeHTML(voice)}" ${voice === selected ? "selected" : ""}>${escapeHTML(voice)} · ${escapeHTML(description)}</option>`
  ).join("");
}

function renderGeminiSettings() {
  const selected = document.getElementById("tts-provider").value;
  const isGemini = selected === "gemini";
  const gemini = geminiProvider();
  for (const id of ["gemini-voice-field", "gemini-tier-field", "gemini-sample-panel"]) {
    document.getElementById(id).classList.toggle("hidden", !isGemini);
  }
  for (const id of ["tts-voice-field", "tts-model-field"]) {
    document.getElementById(id).classList.toggle("hidden", isGemini);
  }
  document.getElementById("gemini-key-note").textContent = gemini?.configured ?
    "Usa la clave guardada para Gemini en Modelos de lenguaje." :
    "Guarda tu clave de Google Gemini en Ajustes → Modelos de lenguaje.";
  document.getElementById("gemini-open-settings").classList.toggle("hidden", Boolean(gemini?.configured));
  const tier = document.getElementById("gemini-tts-tier").value;
  document.getElementById("tts-cost-hint").textContent = isGemini ?
    (tier === "paid" ? "Estimación según los tokens de texto y audio. Google aplica la tarifa del proyecto." :
      "$0.00 (Capa gratuita Gemini), si tu proyecto de Google tiene cuota gratuita disponible.") :
    selected === "edge" ? "Edge-TTS es gratuito y requiere conexión a internet." :
      "La generación usa tu cuenta y puede tener costo según el proveedor y modelo.";
  updateVoiceButtons();
}

function renderProductionView() {
  if (!state.project) return;
  if (state.currentView === "prompts") {
    document.getElementById("prompt-style-label").textContent = state.style ? `Estilo al copiar: ${state.style.name}` : "No hay estilo activo";
    document.getElementById("project-prompts").innerHTML = state.scenes.map((scene) => `
      <div class="prompt-row">
        <div><h2>Escena ${scene.order_index}</h2><p>${escapeHTML(scene.narration)}</p></div>
        <label class="form-field"><span>Prompt visual</span><textarea class="large-textarea" data-edit-prompt="${scene.id}" rows="3" maxlength="3000">${escapeHTML(scene.visual_prompt)}</textarea></label>
        <button class="secondary-button" data-copy-prompt="${scene.id}">Copiar prompt</button>
      </div>`).join("");
  } else if (state.currentView === "resources") {
    const sceneOptions = state.scenes.filter((scene) => !scene.locked).map((scene) => `<option value="${scene.id}">Escena ${scene.order_index}</option>`).join("");
    document.getElementById("project-resources").innerHTML = state.assets.map((asset) => {
      const assigned = state.scenes.filter((scene) => scene.image_url === asset.url).map((scene) => scene.order_index);
      const status = assigned.length ? `Escenas: ${assigned.join(", ")}` : "Sin asignar";
      return `<article class="panel production-card">
        <img class="production-thumb" src="${escapeHTML(asset.url)}" alt="" loading="lazy">
        <div class="production-card-body"><h2>${escapeHTML(asset.name)}</h2><p>${status}</p>
        <form class="media-actions" data-assign-image="${asset.id}"><select class="form-control" name="scene" required aria-label="Escena de destino"><option value="">Seleccionar escena</option>${sceneOptions}</select><button class="secondary-button">Asignar imagen</button></form></div>
      </article>`;
    }).join("") || '<p class="media-hint">Aún no has subido imágenes.</p>';
  } else if (state.currentView === "scenes") {
    const provider = PROVIDER_NAMES[state.project.voice_provider] || "Edge‑TTS";
    document.getElementById("scene-voice-settings").textContent = `Proveedor de voz: ${provider}`;
    document.getElementById("project-scenes").innerHTML = state.scenes.map((scene) => {
      const choice = state.sceneVoiceChoices.get(scene.id) || {};
      const isGemini = (choice.provider || state.project.voice_provider) === "gemini";
      const gemini = geminiProvider();
      return `
      <article class="panel production-card">
        ${scene.image_url ? `<img class="production-thumb" src="${escapeHTML(scene.image_url)}" alt="" loading="lazy">` : '<div class="production-thumb no-image">Sin imagen</div>'}
        <div class="production-card-body">
          <h2>Escena ${scene.order_index}</h2><p class="scene-narration">${escapeHTML(scene.narration)}</p>
          <p>${scene.audio_url ? `${Number(scene.tts_duration).toFixed(3)} s de voz${scene.tts_provider ? ` · ${PROVIDER_NAMES[scene.tts_provider] || ""}` : ""}` : `Sin voz individual · duración en línea de tiempo: ${(scene.end_seconds - scene.start_seconds).toFixed(3)} s`}</p>
          ${scene.audio_url ? `<audio controls preload="none" src="${escapeHTML(scene.audio_url)}" aria-label="Escuchar escena ${scene.order_index}"></audio>` : ""}
          <label class="form-field"><span>Proveedor para esta escena</span><select class="form-control" data-voice-provider="${scene.id}">
            <option value="">Usar ajuste de Audio · ${provider}</option><option value="gemini" ${choice.provider === "gemini" ? "selected" : ""}>Google Gemini TTS</option>
          </select></label>
          <label class="form-field ${isGemini ? "" : "hidden"}"><span>Voz de Gemini</span><select class="form-control" data-scene-gemini-voice="${scene.id}">
            ${geminiVoiceOptions(choice.voice || (state.project.voice_provider === "gemini" ? state.project.voice_name : gemini?.speech?.default_voice))}
          </select></label>
          ${isGemini && !gemini?.configured ? '<button class="secondary-button" data-configure-gemini>Configurar clave de Gemini en Ajustes</button>' : ""}
          <div class="media-actions">
            <button class="secondary-button" data-generate-voice="${scene.id}">${scene.audio_url ? "Regenerar voz" : "Generar voz"}</button>
            <button class="secondary-button" data-upload-image="${scene.id}" ${scene.locked ? "disabled" : ""}>Subir imagen</button>
          </div>
        </div>
      </article>`;
    }).join("");
    updateVoiceButtons();
  } else if (state.currentView === "styles") {
    document.getElementById("project-styles").innerHTML = state.styles.map((style) => `
      <div class="style-preset"><div><h3>${escapeHTML(style.name)}</h3><p>${escapeHTML(style.style_prompt)}</p></div>
      <button class="secondary-button" data-activate-style="${style.id}" ${style.id === state.style?.id ? "disabled" : ""}>${style.id === state.style?.id ? "Activo" : "Usar estilo"}</button></div>`).join("");
  }
}

async function uploadGallery(files) {
  const images = Array.from(files);
  if (!images.length) return;
  const form = new FormData();
  images.forEach((file) => form.append("files", file));
  try {
    const result = await api("/resources/images", { method: "POST", body: form });
    await reloadProject();
    toast(`${result.uploaded} imágenes guardadas en Recursos.`, "success");
  } catch (error) { toast(error.message, "error"); }
}

async function importScript(text, structured, button) {
  if (!text.trim()) return toast("Pega o genera un guion primero.", "error");
  if ((state.scenes.some((scene) => scene.audio_url || scene.image_url) || state.project?.audio_url) &&
      !window.confirm("El guion reemplazará el montaje actual. ¿Continuar?")) return;
  button.disabled = true;
  try {
    const payload = await api("/scenes/apply-script", { method: "POST", body: JSON.stringify({ text, structured }) });
    state.selectedSceneId = payload.selected_scene_id;
    state.sceneVoiceChoices.clear();
    state.currentTime = 0;
    updateProjectFromPayload(payload);
    setView("timeline");
    renderTimeline();
    toast(`${state.scenes.length} escenas guardadas en el proyecto.`, "success");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; }
}

function renderSettings() {
  return { aspect_ratio: document.getElementById("render-aspect").value, subtitle_mode: document.getElementById("subtitle-mode").value };
}

async function persistVoiceSettings() {
  const settings = voiceSettings();
  try {
    await api("/audio/voice", { method: "PUT", body: JSON.stringify(settings) });
    Object.assign(state.project, { voice_provider: settings.provider, voice_name: settings.voice, voice_model: settings.model,
      gemini_tts_tier: settings.gemini_tts_tier ?? state.project.gemini_tts_tier });
    updateVoiceButtons();
  } catch (error) { toast(error.message, "error"); }
}

function bindProductionEvents() {
  document.querySelectorAll("[data-script-mode]").forEach((tab) => tab.addEventListener("click", () => {
    const paste = tab.dataset.scriptMode === "paste";
    document.getElementById("script-paste-panel").classList.toggle("hidden", !paste);
    document.getElementById("script-api-panel").classList.toggle("hidden", paste);
    document.querySelectorAll("[data-script-mode]").forEach((item) => {
      item.classList.toggle("active", item === tab);
      item.setAttribute("aria-pressed", String(item === tab));
    });
  }));
  document.getElementById("import-structured-script").addEventListener("click", (event) =>
    importScript(document.getElementById("structured-script").value, true, event.currentTarget));
  document.getElementById("use-script").addEventListener("click", (event) =>
    importScript(document.getElementById("script-output").value, false, event.currentTarget));

  const prompts = document.getElementById("project-prompts");
  prompts.addEventListener("change", async (event) => {
    const input = event.target.closest("[data-edit-prompt]");
    const scene = state.scenes.find((item) => item.id === Number(input?.dataset.editPrompt));
    if (!scene) return;
    try {
      const saved = await api(`/scenes/${scene.id}/prompt`, { method: "PATCH", body: JSON.stringify({ visual_prompt: input.value }) });
      scene.visual_prompt = saved.visual_prompt;
      toast("Prompt guardado.", "success");
    } catch (error) { toast(error.message, "error"); }
  });
  prompts.addEventListener("click", (event) => {
    const scene = state.scenes.find((item) => item.id === Number(event.target.closest("[data-copy-prompt]")?.dataset.copyPrompt));
    if (scene) copyText(styledPrompt(scene), "Prompt copiado.").catch((error) => toast(error.message, "error"));
  });
  document.getElementById("copy-all-prompts").addEventListener("click", () => {
    const text = state.scenes.map((scene) => {
      const prompt = styledPrompt(scene);
      return prompt ? `ESCENA ${scene.order_index}\n${prompt}` : "";
    }).filter(Boolean).join("\n\n");
    copyText(text, "Prompts copiados.").catch((error) => toast(error.message, "error"));
  });

  const picker = document.getElementById("resource-files");
  picker.addEventListener("change", () => { uploadGallery(picker.files); picker.value = ""; });
  const drop = document.getElementById("resource-drop");
  drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("drop-active"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drop-active"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("drop-active");
    uploadGallery(event.dataTransfer.files);
  });
  document.getElementById("auto-assign-images").addEventListener("click", async () => {
    try {
      const result = await api("/resources/auto-assign", { method: "POST" });
      await reloadProject();
      toast(result.assigned ? `${result.assigned} imágenes asignadas.` : "No hay imágenes disponibles para escenas libres.", result.assigned ? "success" : "info");
    } catch (error) { toast(error.message, "error"); }
  });
  document.getElementById("project-resources").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      updateProjectFromPayload(await api(`/resources/${form.dataset.assignImage}/assign`, {
        method: "PUT", body: JSON.stringify({ scene_id: Number(form.elements.scene.value) }),
      }));
      toast("Imagen asignada a la escena.", "success");
    } catch (error) { toast(error.message, "error"); }
  });

  const sceneGrid = document.getElementById("project-scenes");
  sceneGrid.addEventListener("change", (event) => {
    const provider = event.target.dataset.voiceProvider;
    const voice = event.target.dataset.sceneGeminiVoice;
    if (provider) {
      state.sceneVoiceChoices.set(Number(provider), { provider: event.target.value });
      renderProductionView();
    } else if (voice) {
      const id = Number(voice);
      state.sceneVoiceChoices.set(id, { ...state.sceneVoiceChoices.get(id), voice: event.target.value });
    }
  });
  sceneGrid.addEventListener("click", (event) => {
    if (event.target.closest("[data-configure-gemini]")) return setView("settings");
    const voice = event.target.closest("[data-generate-voice]");
    const image = event.target.closest("[data-upload-image]");
    if (voice) generateTts({ sceneId: Number(voice.dataset.generateVoice) });
    if (image) {
      const scene = state.scenes.find((item) => item.id === Number(image.dataset.uploadImage));
      const input = document.createElement("input");
      input.type = "file";
      input.accept = ".png,.jpg,.jpeg,.webp";
      input.addEventListener("change", () => uploadImage(input.files[0], scene, false), { once: true });
      input.click();
    }
  });
  document.getElementById("style-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      updateProjectFromPayload(await api("/styles", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form))) }));
      form.reset();
      toast("Preset guardado y seleccionado.", "success");
    } catch (error) { toast(error.message, "error"); }
  });
  document.getElementById("project-styles").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-activate-style]");
    if (!button) return;
    try {
      updateProjectFromPayload(await api(`/styles/${button.dataset.activateStyle}/select`, { method: "PUT" }));
      toast("Estilo seleccionado.", "success");
    } catch (error) { toast(error.message, "error"); }
  });
}

function renderTimeline() {
  const track = document.getElementById("video-track");
  const total = state.project?.duration_seconds || 0;
  const viewport = document.getElementById("timeline-scroller").clientWidth;
  const pixelsPerSecond = Math.max(18, (viewport - 100) / Math.max(1, total));
  state.timelineScale = pixelsPerSecond;
  const trackWidth = total * pixelsPerSecond;
  const ruler = document.getElementById("timeline-ruler");
  track.style.width = ruler.style.width = `${trackWidth}px`;
  document.querySelector(".audio-track").style.width = `${trackWidth}px`;
  const step = total > 60 ? 5 : total > 15 ? 2 : 1;
  const ticks = [];
  for (let sec = 0; sec <= total; sec += step) {
    ticks.push(`<span style="left:${sec * pixelsPerSecond}px">${formatTime(sec)}</span>`);
  }
  ruler.innerHTML = ticks.join("");
  track.innerHTML = state.scenes.map((item) => {
    const duration = item.end_seconds - item.start_seconds;
    const width = duration * pixelsPerSecond;
    const thumbnail = item.image_url ? `<img class="timeline-thumbnail" src="${escapeHTML(item.image_url)}" alt="">` : "";
    return `<button class="scene-clip ${item.id === state.selectedSceneId ? "selected" : ""} ${item.locked ? "locked" : ""}" data-scene-id="${item.id}" style="width:${width}px" title="Escena ${item.order_index} · ${formatTime(item.start_seconds, 2)} a ${formatTime(item.end_seconds, 2)}"><span class="clip-art">${thumbnail}</span><span class="clip-label">#${String(item.order_index).padStart(3, "0")}</span><span class="clip-duration">${duration.toFixed(1)}s</span></button>`;
  }).join("");
  track.querySelectorAll(".scene-clip").forEach((button) => button.addEventListener("click", () => chooseScene(Number(button.dataset.sceneId), true)));
  renderTimelineCursor();
}

function renderTimelineCursor() {
  const scene = selectedScene();
  const playhead = document.getElementById("playhead");
  playhead.style.left = `calc(var(--track-label-width) + ${state.currentTime * state.timelineScale}px)`;
  document.getElementById("current-time").textContent = formatTime(state.currentTime, 2);
  document.getElementById("cursor-time").textContent = formatTime(state.currentTime, 2);
  document.getElementById("review-timecode").textContent = `${formatTime(state.currentTime)} / ${formatTime(state.project?.duration_seconds || 0)}`;
  document.getElementById("composer-timestamp").textContent = formatTime(state.currentTime, 2);
  if (scene) document.getElementById("composer-scene").textContent = `· Escena ${String(scene.order_index).padStart(2, "0")}`;
}

function renderLibrary() {
  const grid = document.getElementById("library-grid");
  const scenesWithImages = state.scenes.filter((scene) => scene.image_url);
  if (!scenesWithImages.length) {
    grid.innerHTML = `<div class="library-empty">Todavía no hay imágenes subidas a la biblioteca.<br><br><span>Selecciona una escena y pulsa “Subir archivo”.</span></div>`;
    return;
  }
  grid.innerHTML = scenesWithImages.map((scene) => `<button class="library-tile" data-scene-id="${scene.id}"><img src="${scene.image_url}" alt="Recurso escena ${scene.order_index}"><span>ESCENA ${String(scene.order_index).padStart(2, "0")}</span></button>`).join("");
  grid.querySelectorAll(".library-tile").forEach((tile) => tile.addEventListener("click", () => chooseScene(Number(tile.dataset.sceneId), false)));
}

function renderReview() {
  const pendingCount = state.notes.filter((note) => note.status === "pendiente").length;
  document.getElementById("review-count-number").textContent = pendingCount;
  document.getElementById("review-notes-subtitle").textContent = `${state.notes.length} ${state.notes.length === 1 ? "nota guardada" : "notas guardadas"} · ${state.notes.length - pendingCount} aplicadas`;
  const list = document.getElementById("review-notes-list");
  document.getElementById("composer-timestamp").textContent = formatTime(state.currentTime, 2);
  document.getElementById("review-timecode").textContent = `${formatTime(state.currentTime)} / ${formatTime(state.project?.duration_seconds || 0)}`;
  const activeScene = getSceneAtTime(state.currentTime);
  if (activeScene) document.getElementById("composer-scene").textContent = `· Escena ${String(activeScene.order_index).padStart(2, "0")}`;
  if (!state.notes.length) {
    list.innerHTML = `<div class="empty-notes">No hay notas todavía. Pausa la vista previa y comenta un momento.</div>`;
    return;
  }
  list.innerHTML = state.notes.map((note) => {
    const scene = state.scenes.find((item) => item.id === note.scene_id);
    return `<article class="review-note-card"><div class="review-note-card-top"><strong>${formatTime(note.time_seconds)}</strong><span>${scene ? `Escena ${String(scene.order_index).padStart(2, "0")}` : "Proyecto"}</span><span class="note-status ${note.status === "aplicada" ? "applied" : ""}">${note.status === "aplicada" ? "✓ aplicada" : "pendiente"}</span></div><p>${escapeHTML(note.note)}</p></article>`;
  }).join("");
}

function chooseScene(sceneId, jumpToStart) {
  const scene = state.scenes.find((item) => item.id === sceneId);
  if (!scene) return;
  state.selectedSceneId = scene.id;
  if (jumpToStart) {
    state.currentTime = scene.start_seconds;
    const audio = document.getElementById("timeline-audio");
    if (audio.hasAttribute("src")) audio.currentTime = scene.start_seconds;
  }
  renderInspector();
}

async function reloadProject() {
  const payload = await api("/bootstrap");
  updateProjectFromPayload(payload);
}

function renderMediaPanels() {
  if (!state.project) return;
  const project = state.project;
  const costs = state.costs || {};
  const money = (value) => `$${Number(value || 0).toFixed(2)}`;
  const audioUrl = project.audio_url;
  const duration = Number(project.duration_seconds || 0);
  document.getElementById("audio-duration-label").textContent = audioUrl ? `${formatTime(duration, 2)} · ${duration.toFixed(2)} s` : "Sin audio";
  document.getElementById("audio-source-label").textContent = audioUrl ? "Narración del proyecto" : "—";
  const timelineAudio = document.getElementById("timeline-audio");
  if (audioUrl && timelineAudio.getAttribute("src") !== audioUrl) timelineAudio.src = audioUrl;
  if (!audioUrl && timelineAudio.hasAttribute("src")) {
    timelineAudio.removeAttribute("src");
    timelineAudio.load();
  }
  document.getElementById("timeline-audio-label").textContent = audioUrl ? `Narración principal · ${formatTime(duration)}` : "Sin narración";
  document.getElementById("waveform").classList.toggle("hidden", !audioUrl);
  document.getElementById("music-file-label").textContent = project.music_path ? `Música: ${project.music_path.split(/[\\/]/).pop()}` : "Añadir música local";
  document.getElementById("logo-file-label").textContent = project.logo_path ? `Logo: ${project.logo_path.split(/[\\/]/).pop()}` : "Añadir logotipo PNG";
  document.getElementById("tts-provider").value = project.voice_provider || "edge";
  document.getElementById("tts-model").value = project.voice_model || "";
  document.getElementById("tts-voice").value = project.voice_name || (project.voice_provider === "edge" ? "es-CO-SalomeNeural" : "");
  document.getElementById("gemini-tts-tier").value = project.gemini_tts_tier || "free";
  const gemini = geminiProvider();
  document.getElementById("gemini-voice").innerHTML = geminiVoiceOptions(project.voice_provider === "gemini" ? project.voice_name : gemini?.speech?.default_voice);
  renderGeminiSettings();
  document.getElementById("image-provider").value = project.image_provider || "openai";
  document.getElementById("image-model").value = project.image_model || "gpt-image-2.5-flare";
  document.getElementById("render-aspect").value = project.aspect_ratio || "16:9";
  document.getElementById("subtitle-mode").value = project.subtitle_mode || "none";
  updateVoiceButtons();
  document.getElementById("music-gain").value = project.music_gain_db ?? -15;
  document.getElementById("voice-gain").value = project.voice_gain_db ?? 0;
  document.getElementById("sfx-gain").value = project.sfx_gain_db ?? 0;
  document.getElementById("music-ducking").checked = Boolean(project.music_ducking ?? 1);
  document.getElementById("logo-position").value = project.logo_position || "bottom_right";
  document.getElementById("logo-scale").value = project.logo_scale ?? 0.14;
  document.getElementById("logo-opacity").value = project.logo_opacity ?? 0.85;
  document.getElementById("cartesia-rate").value = project.cartesia_usd_per_1000_chars ?? 0;
  document.getElementById("cost-images").textContent = money(costs.image_cost);
  document.getElementById("cost-voice").textContent = money(costs.voice_cost);
  document.getElementById("cost-llm").textContent = money(costs.llm_cost);
  document.getElementById("cost-total").textContent = money(costs.total_cost);
  document.getElementById("export-llm-tokens").textContent = `${Number(costs.llm_tokens || 0).toLocaleString("es-CO")} tok`;
  document.getElementById("export-image-count").textContent = `${state.scenes.filter((scene) => scene.image_path).length}/${state.scenes.length}`;
  document.getElementById("export-voice-cost").textContent = money(costs.voice_cost);
  document.getElementById("export-total-cost").textContent = money(costs.total_cost);
  document.getElementById("last-export-label").textContent = project.last_export_path ? `Exportación lista: ${project.last_export_path.split(/[\\/]/).pop()}` : "No hay una exportación reciente.";
  const effects = document.getElementById("sfx-list");
  effects.innerHTML = state.soundEffects.length ? state.soundEffects.map((effect) => `<div class="sfx-row"><span>${escapeHTML(effect.name)}</span><small>${Number(effect.start_seconds).toFixed(1)} s</small></div>`).join("") : `<span class="media-hint">Sin efectos añadidos.</span>`;
  document.getElementById("export-result").classList.toggle("hidden", !project.last_export_url);
  const srt = document.getElementById("download-srt");
  srt.classList.toggle("hidden", !project.last_subtitle_url);
  if (project.last_subtitle_url) srt.href = project.last_subtitle_url;
  else srt.removeAttribute("href");
  if (project.last_export_url) {
    const exportVideo = document.getElementById("export-video");
    if (exportVideo.getAttribute("src") !== project.last_export_url) exportVideo.src = project.last_export_url;
    document.getElementById("download-video").href = project.last_export_url;
    document.getElementById("export-result").classList.remove("hidden");
    const reviewVideo = document.getElementById("review-video");
    if (reviewVideo.src !== new URL(project.last_export_url, location.href).href) reviewVideo.src = project.last_export_url;
    reviewVideo.classList.remove("hidden");
  } else {
    document.getElementById("review-video").classList.add("hidden");
  }
}

async function waitForJob(jobId, statusId) {
  const status = document.getElementById(statusId);
  status.classList.remove("hidden");
  for (;;) {
    const job = await api(`/jobs/${encodeURIComponent(jobId)}`);
    status.textContent = job.status === "running" ? `${job.message} · ${Number(job.progress || 0)}%` : job.message;
    if (job.status === "error") throw new Error(job.error || job.message);
    if (job.status === "complete") return job.result || {};
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}

function voiceSettings() {
  const provider = document.getElementById("tts-provider").value;
  if (provider === "gemini") {
    return { provider, model: geminiProvider()?.speech?.model || "",
      voice: document.getElementById("gemini-voice").value,
      gemini_tts_tier: document.getElementById("gemini-tts-tier").value };
  }
  return {
    provider,
    voice: document.getElementById("tts-voice").value.trim(),
    model: document.getElementById("tts-model").value.trim(),
  };
}

function updateVoiceButtons() {
  const missingGeminiKey = !geminiProvider()?.configured;
  const geminiSelected = document.getElementById("tts-provider").value === "gemini";
  document.getElementById("generate-tts").disabled = state.voiceBusy || (geminiSelected && missingGeminiKey);
  document.getElementById("gemini-voice-sample").disabled = state.voiceBusy || missingGeminiKey;
  const button = document.getElementById("generate-pending-voices");
  const pending = state.scenes.filter((scene) => !scene.audio_url).length;
  button.disabled = state.voiceBusy || pending === 0;
  button.title = `${pending} escenas pendientes · ${state.project?.voice_provider || "edge"}`;
  document.querySelectorAll("[data-generate-voice]").forEach((item) => {
    const provider = state.sceneVoiceChoices.get(Number(item.dataset.generateVoice))?.provider || state.project?.voice_provider;
    item.disabled = state.voiceBusy || (provider === "gemini" && missingGeminiKey);
  });
}

async function generateTts({ sceneId = null, pendingOnly = false } = {}) {
  if (state.voiceBusy) return;
  const settings = { ...voiceSettings(), scene_id: sceneId, pending_only: pendingOnly };
  if (sceneId) {
    const choice = state.sceneVoiceChoices.get(sceneId);
    if ((choice?.provider || settings.provider) === "gemini") {
      Object.assign(settings, { provider: "gemini", model: geminiProvider()?.speech?.model || "",
        voice: document.querySelector(`[data-scene-gemini-voice="${sceneId}"]`)?.value || "",
        gemini_tts_tier: state.project.gemini_tts_tier });
    }
  }
  if (settings.provider === "gemini" && !geminiProvider()?.configured) {
    toast("Guarda tu clave de Google Gemini en Ajustes → Modelos de lenguaje.", "error");
    setView("settings");
    return;
  }
  state.voiceBusy = true;
  updateVoiceButtons();
  try {
    const { job_id } = await api("/audio/tts", { method: "POST", body: JSON.stringify(settings) });
    if (!job_id) return toast("Todas las escenas tienen voz.");
    const result = await waitForJob(job_id, "audio-job-status");
    await reloadProject();
    const cost = result.provider === "gemini" && result.gemini_tts_tier === "free" ?
      "$0.00 (Capa gratuita Gemini)" : `${moneyUSD(result.estimated_cost)} estimados`;
    toast(`Voz lista: ${result.scenes} escenas · ${result.duration_seconds.toFixed(2)} s · ${cost}.`, "success");
  } catch (error) {
    await reloadProject().catch(() => {});
    toast(error.message, "error");
  } finally {
    state.voiceBusy = false;
    updateVoiceButtons();
    document.getElementById("audio-job-status").classList.add("hidden");
  }
}

function clearGeminiSample() {
  const player = document.getElementById("gemini-sample-player");
  player.pause();
  player.removeAttribute("src");
  player.classList.add("hidden");
  player.load();
}

async function playGeminiSample() {
  if (state.voiceBusy) return;
  const settings = voiceSettings();
  state.voiceBusy = true;
  updateVoiceButtons();
  try {
    const { job_id } = await api("/audio/voice/sample", { method: "POST", body: JSON.stringify(settings) });
    const result = await waitForJob(job_id, "audio-job-status");
    await reloadProject();
    if (state.currentView === "audio" && voiceSettings().provider === "gemini" && voiceSettings().voice === result.voice) {
      const player = document.getElementById("gemini-sample-player");
      player.src = result.audio_url;
      player.classList.remove("hidden");
      await player.play().catch(() => {});
    }
  } catch (error) { toast(error.message, "error"); }
  finally {
    state.voiceBusy = false;
    updateVoiceButtons();
    document.getElementById("audio-job-status").classList.add("hidden");
  }
}

function moneyUSD(value) { return `$${Number(value || 0).toFixed(4)}`; }

async function uploadAudioAsset(inputId, endpoint, successLabel) {
  const input = document.getElementById(inputId);
  const file = input.files[0];
  if (!file) return;
  const form = new FormData();
  form.set("file", file);
  try {
    const result = await api(endpoint, { method: "POST", body: form });
    if (result.sync_job_id) {
      await waitForJob(result.sync_job_id, "sync-job-status");
      toast("Audio maestro sincronizado con las escenas usando faster-whisper.", "success");
    } else toast(successLabel, "success");
    input.value = "";
    await reloadProject();
  } catch (error) { toast(error.message, "error"); }
}

async function saveAudioSettings() {
  const payload = {
    voice_gain_db: Number(document.getElementById("voice-gain").value),
    music_gain_db: Number(document.getElementById("music-gain").value),
    sfx_gain_db: Number(document.getElementById("sfx-gain").value),
    music_ducking: document.getElementById("music-ducking").checked,
    logo_position: document.getElementById("logo-position").value,
    logo_scale: Number(document.getElementById("logo-scale").value),
    logo_opacity: Number(document.getElementById("logo-opacity").value),
    cartesia_usd_per_1000_chars: Number(document.getElementById("cartesia-rate").value),
  };
  try {
    await api("/audio/settings", { method: "PUT", body: JSON.stringify(payload) });
    Object.assign(state.project, payload);
    toast("Ajustes de mezcla guardados.", "success");
  }
  catch (error) { toast(error.message, "error"); }
}

async function addSoundEffect() {
  const file = document.getElementById("sfx-file").files[0];
  if (!file) return toast("Selecciona un archivo de audio para el efecto.", "error");
  const form = new FormData();
  form.set("file", file);
  form.set("start_seconds", document.getElementById("sfx-start").value || "0");
  try { await api("/audio/sfx", { method: "POST", body: form }); document.getElementById("sfx-file").value = ""; await reloadProject(); toast("Efecto agregado a la mezcla.", "success"); }
  catch (error) { toast(error.message, "error"); }
}

async function generateImages(missingOnly, sceneId = null) {
  const button = sceneId ? document.getElementById("regenerate-image") : document.getElementById(missingOnly ? "generate-missing-images" : "generate-all-images");
  if (button) button.disabled = true;
  try {
    const payload = { provider: document.getElementById("image-provider").value, model: document.getElementById("image-model").value.trim(), missing_only: missingOnly, scene_id: sceneId, correction: sceneId ? document.getElementById("image-correction").value.trim() : "" };
    const { job_id } = await api("/images/generate", { method: "POST", body: JSON.stringify(payload) });
    const result = await waitForJob(job_id, "image-job-status");
    await reloadProject();
    if (sceneId) document.getElementById("image-correction").value = "";
    toast(`${result.images} imagen(es) generada(s) · ${moneyUSD(result.estimated_cost)} estimados.`, "success");
  } catch (error) { toast(error.message, "error"); }
  finally { if (button) button.disabled = false; }
}

async function renderVideo() {
  const button = document.getElementById("render-video");
  if (button) button.disabled = true;
  try {
    const { job_id } = await api("/render", { method: "POST", body: JSON.stringify(renderSettings()) });
    const result = await waitForJob(job_id, "render-job-status");
    await reloadProject();
    document.getElementById("export-video").src = result.video_url;
    document.getElementById("download-video").href = result.video_url;
    document.getElementById("export-result").classList.remove("hidden");
    toast(`MP4 listo · ${result.width}×${result.height} · ${result.duration.toFixed(2)} s · ${result.applied_notes} nota(s) aplicada(s).`, "success");
  } catch (error) { toast(error.message, "error"); }
  finally { if (button) button.disabled = false; }
}

async function loadProviders() {
  try {
    const payload = await api("/providers");
    state.providers = payload.providers;
    const voices = document.getElementById("gemini-voice");
    voices.innerHTML = geminiVoiceOptions(state.project?.voice_provider === "gemini" ?
      state.project.voice_name : geminiProvider()?.speech?.default_voice);
    renderGeminiSettings();
    if (state.currentView === "scenes") renderProductionView();
    renderHeader();
    renderProviders();
  } catch (error) { toast(error.message, "error"); }
}

function renderProviders() {
  const containers = { llm: document.getElementById("provider-group-llm"), media: document.getElementById("provider-group-media"), audio: document.getElementById("provider-group-audio") };
  Object.values(containers).forEach((container) => container.replaceChildren());
  state.providers.forEach((provider) => {
    const presentation = providerPresentation[provider.id];
    const container = containers[presentation.group];
    const lastCheck = provider.last_check;
    const logoClass = presentation.tone;
    const status = provider.configured ? (lastCheck?.ok ? "Conectada" : "Clave guardada") : "Sin configurar";
    const statusClass = provider.configured ? (lastCheck?.ok ? "connected" : "") : "";
    const messageText = lastCheck ? lastCheck.message : (provider.configured ? provider.note : provider.note);
    const models = state.providerModels[provider.id] || [];
    const card = document.createElement("article");
    card.className = "provider-card";
    card.dataset.providerId = provider.id;
    card.innerHTML = `
      <div class="provider-top"><span class="provider-logo ${logoClass}">${escapeHTML(presentation.mark)}</span><div class="provider-title"><strong>${escapeHTML(provider.label)}</strong><small>${escapeHTML(provider.category)}</small></div><span class="provider-status ${statusClass}" data-role="status">${status}</span></div>
      <div class="provider-desc">${escapeHTML(provider.note)}</div>
      <div class="provider-key-row"><input class="provider-key-input" type="password" autocomplete="new-password" spellcheck="false" aria-label="${escapeHTML(provider.secret_label)} de ${escapeHTML(provider.label)}" placeholder="${provider.configured ? "••••••••••••••••  ·  Guardada" : `Pega aquí tu ${provider.secret_label}`}" data-role="secret"><button class="save-key-button" data-action="save">Guardar</button></div>
      <div class="provider-footer"><span class="provider-message ${lastCheck?.ok ? "success" : ""}" data-role="message">${escapeHTML(messageText)}</span><span class="provider-actions"><button class="test-key-button" data-action="test" ${provider.testable && provider.configured ? "" : "disabled"} title="${provider.testable ? "Comprueba la clave sin generar contenido" : "No disponible en esta versión"}">${provider.testable ? "Probar conexión" : "Validación no disponible"}</button>${provider.configured ? `<button class="remove-key-button" data-action="remove">Quitar</button>` : ""}</span></div>
      ${models.length ? `<select class="provider-test-models" data-role="models" aria-label="Modelos encontrados">${models.map((model) => `<option value="${escapeHTML(model)}">${escapeHTML(model)}</option>`).join("")}</select>` : ""}`;
    container.append(card);
    const input = card.querySelector('[data-role="secret"]');
    card.querySelector('[data-action="save"]').addEventListener("click", () => saveProviderSecret(provider.id, input.value, card));
    const test = card.querySelector('[data-action="test"]');
    test.addEventListener("click", () => testProvider(provider.id, card, test));
    const remove = card.querySelector('[data-action="remove"]');
    if (remove) remove.addEventListener("click", () => removeProviderSecret(provider.id));
    const modelsSelect = card.querySelector('[data-role="models"]');
    if (modelsSelect) modelsSelect.addEventListener("change", () => {
      state.providerModels[provider.id] = Array.from(modelsSelect.options).map((option) => option.value);
      localStorage.setItem("studio-provider-models", JSON.stringify(state.providerModels));
      syncScriptProvider();
    });
  });
}

async function saveProviderSecret(providerId, secret, card) {
  if (!secret.trim()) {
    toast("Pega una clave nueva en el campo para guardarla.", "error");
    return;
  }
  const button = card.querySelector('[data-action="save"]');
  const message = card.querySelector('[data-role="message"]');
  button.disabled = true;
  button.textContent = "Guardando";
  try {
    await api(`/providers/${providerId}/secret`, { method: "PUT", body: JSON.stringify({ secret }) });
    card.querySelector('[data-role="secret"]').value = "";
    message.textContent = "Clave guardada de forma segura. Ahora puedes probarla.";
    message.className = "provider-message success";
    toast("Clave guardada en el almacén seguro de Windows.", "success");
    await loadProviders();
  } catch (error) {
    message.textContent = error.message;
    message.className = "provider-message error";
    toast(error.message, "error");
  } finally { button.disabled = false; button.textContent = "Guardar"; }
}

async function testProvider(providerId, card, button) {
  const message = card.querySelector('[data-role="message"]');
  const status = card.querySelector('[data-role="status"]');
  button.disabled = true;
  button.textContent = "Probando…";
  status.textContent = "Conectando";
  try {
    const result = await api(`/providers/${providerId}/test`, { method: "POST", body: "{}" });
    message.textContent = `${result.message} · ${result.elapsed_ms} ms`;
    message.className = "provider-message success";
    status.textContent = "Conectada";
    status.className = "provider-status connected";
    if (result.models?.length) {
      state.providerModels[providerId] = result.models;
      localStorage.setItem("studio-provider-models", JSON.stringify(state.providerModels));
      renderProviders();
      syncScriptProvider();
      toast(`Conexión lista. ${result.models.length} modelos disponibles.`, "success");
      return;
    }
    toast(result.message, "success");
  } catch (error) {
    message.textContent = error.message;
    message.className = "provider-message error";
    status.textContent = "Revisar clave";
    status.className = "provider-status";
    toast(error.message, "error");
  } finally {
    const refreshed = document.querySelector(`.provider-card[data-provider-id="${providerId}"] [data-action="test"]`);
    if (refreshed) { refreshed.disabled = false; refreshed.textContent = "Probar conexión"; }
    else { button.disabled = false; button.textContent = "Probar conexión"; }
  }
}

async function removeProviderSecret(providerId) {
  try {
    await api(`/providers/${providerId}/secret`, { method: "DELETE" });
    state.providerModels[providerId] = [];
    localStorage.setItem("studio-provider-models", JSON.stringify(state.providerModels));
    toast("Clave eliminada del almacén seguro.", "success");
    await loadProviders();
  } catch (error) { toast(error.message, "error"); }
}

function syncScriptProvider() {
  const providerInput = document.getElementById("script-provider");
  if (!providerInput) return;
  const providerId = providerInput.value;
  const modelInput = document.getElementById("script-model");
  const models = state.providerModels[providerId] || [];
  if (models.length) {
    modelInput.value = models[0];
    modelInput.setAttribute("list", `models-${providerId}`);
    let datalist = document.getElementById(`models-${providerId}`);
    if (!datalist) { datalist = document.createElement("datalist"); datalist.id = `models-${providerId}`; modelInput.insertAdjacentElement("afterend", datalist); }
    datalist.innerHTML = models.map((model) => `<option value="${escapeHTML(model)}">`).join("");
  } else {
    modelInput.value = "";
    modelInput.removeAttribute("list");
    modelInput.placeholder = "Prueba la conexión para cargar los modelos disponibles";
  }
}

async function saveScene() {
  const scene = selectedScene();
  if (!scene) return;
  try {
    const payload = await api(`/scenes/${scene.id}`, { method: "PUT", body: JSON.stringify({ narration: document.getElementById("narration-input").value, visual_prompt: document.getElementById("visual-prompt").value, locked: scene.locked }) });
    updateProjectFromPayload(payload);
    toast("Cambios guardados en el proyecto local.", "success");
  } catch (error) { toast(error.message, "error"); }
}

function updateProjectFromPayload(payload) {
  state.project = payload.project;
  state.scenes = payload.scenes;
  state.notes = payload.notes;
  state.costs = payload.costs;
  state.soundEffects = payload.sound_effects || [];
  state.assets = payload.assets || [];
  state.styles = payload.styles || [];
  state.style = payload.style;
  if (!state.scenes.some((scene) => scene.id === state.selectedSceneId)) state.selectedSceneId = state.scenes[0]?.id;
  state.currentTime = Math.min(state.currentTime, state.project.duration_seconds);
  renderHeader();
  renderInspector();
  renderReview();
  renderMediaPanels();
  renderProductionView();
  renderWorkspace();
}

async function uploadImage(file, scene = selectedScene(), showLibrary = true) {
  if (!scene || !file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    await api(`/scenes/${scene.id}/image`, { method: "POST", body: form });
    await reloadProject();
    if (showLibrary) document.querySelector('[data-inspector-tab="library"]').click();
    toast(`Imagen asignada a la escena ${String(scene.order_index).padStart(2, "0")}.`, "success");
  } catch (error) { toast(error.message, "error"); }
}

async function saveReviewNote() {
  const input = document.getElementById("review-note-input");
  const scene = selectedScene();
  if (!input.value.trim()) { toast("Escribe una instrucción antes de guardar la nota.", "error"); return; }
  try {
    await api("/review-notes", { method: "POST", body: JSON.stringify({ scene_id: scene?.id || null, time_seconds: state.currentTime, note: input.value }) });
    input.value = "";
    await reloadProject();
    renderReview();
    toast("Nota de revisión guardada con su marca de tiempo.", "success");
  } catch (error) { toast(error.message, "error"); }
}

async function applyReviewNotes() {
  const pending = state.notes.filter((note) => note.status === "pendiente");
  if (!pending.length) { toast("No hay notas pendientes por aplicar."); return; }
  setView("export");
  await renderVideo();
}

async function generateScript() {
  const button = document.getElementById("generate-script");
  const output = document.getElementById("script-output");
  const status = document.getElementById("script-output-status");
  const provider = document.getElementById("script-provider").value;
  const model = document.getElementById("script-model").value.trim();
  const prompt = document.getElementById("script-prompt").value.trim();
  if (!model) { toast("Prueba la conexión del proveedor para cargar un modelo.", "error"); return; }
  if (!prompt) { toast("Escribe el tema del guion.", "error"); return; }
  button.disabled = true;
  button.textContent = "Generando…";
  status.textContent = "Esperando respuesta del proveedor…";
  try {
    const result = await api("/ai/script", { method: "POST", body: JSON.stringify({ provider, model, prompt }) });
    output.value = result.text;
    document.getElementById("script-token-count").textContent = `${result.tokens.toLocaleString("es-CO")} tokens · ${result.provider} · ${moneyUSD(result.estimated_cost)} estimados`;
    status.textContent = `Respuesta recibida de ${result.provider} · ${result.model}`;
    await reloadProject();
    toast("Guion generado y uso de tokens registrado en el proyecto.", "success");
  } catch (error) {
    status.textContent = "No se pudo completar la solicitud.";
    toast(error.message, "error");
  } finally { button.disabled = false; button.innerHTML = "<span>✧</span> Generar guion"; }
}

async function mutateScene(action) {
  const scene = selectedScene();
  if (!scene) return;
  try {
    const payload = await api(`/scenes/${scene.id}/${action}`, { method: "POST", body: "{}" });
    const previous = scene.order_index;
    state.project = payload.project;
    state.scenes = payload.scenes;
    state.notes = payload.notes;
    state.selectedSceneId = payload.selected_scene_id || state.scenes[Math.min(previous - 1, state.scenes.length - 1)]?.id;
    if (action === "split") toast("Escena dividida en dos segmentos.", "success");
    if (action === "merge") toast("Escena fusionada con la anterior.", "success");
    if (action === "add") toast("Nueva escena agregada a la línea de tiempo.", "success");
    if (action === "delete") toast("Escena quitada de la línea de tiempo.", "success");
    updateProjectFromPayload(payload);
  } catch (error) { toast(error.message, "error"); }
}

function setCurrentTime(value, seek = true) {
  state.currentTime = Math.max(0, Math.min(Number(value), state.project?.duration_seconds || 0));
  const reviewVideo = document.getElementById("review-video");
  if (state.currentView === "review" && !reviewVideo.classList.contains("hidden") && Math.abs(reviewVideo.currentTime - state.currentTime) > 0.1) reviewVideo.currentTime = state.currentTime;
  const audio = document.getElementById("timeline-audio");
  if (seek && state.currentView === "timeline" && audio.hasAttribute("src")) audio.currentTime = state.currentTime;
  document.getElementById("review-timecode").textContent = `${formatTime(state.currentTime)} / ${formatTime(state.project?.duration_seconds || 0)}`;
  const active = getSceneAtTime(state.currentTime);
  if (active && active.id !== state.selectedSceneId) {
    state.selectedSceneId = active.id;
    renderInspector();
  } else renderTimelineCursor();
}

function togglePlayback(buttonId = "play-button") {
  const reviewVideo = document.getElementById("review-video");
  if (state.currentView === "review" && !reviewVideo.classList.contains("hidden")) {
    if (reviewVideo.paused) reviewVideo.play().catch(() => toast("No se pudo reproducir el MP4 local.", "error"));
    else reviewVideo.pause();
    return;
  }
  const audio = document.getElementById("timeline-audio");
  if (state.currentView === "timeline" && audio.hasAttribute("src")) {
    if (!audio.paused) audio.pause();
    else {
      audio.currentTime = state.currentTime < state.project.duration_seconds ? state.currentTime : 0;
      audio.play().catch(() => toast("No se pudo reproducir la narración.", "error"));
    }
    return;
  }
  state.playing = !state.playing;
  const label = state.playing ? "Ⅱ" : "▶";
  document.getElementById("play-button").textContent = label;
  document.getElementById("review-play").textContent = label;
  if (state.playing) {
    clearInterval(state.timer);
    state.timer = setInterval(() => {
      const end = state.project?.duration_seconds || 79;
      if (state.currentTime >= end) { state.currentTime = 0; state.playing = false; clearInterval(state.timer); document.getElementById("play-button").textContent = "▶"; document.getElementById("review-play").textContent = "▶"; }
      else state.currentTime += 0.2;
      const active = getSceneAtTime(state.currentTime);
      if (active && active.id !== state.selectedSceneId) { state.selectedSceneId = active.id; renderInspector(); }
      else renderTimelineCursor();
    }, 200);
  } else clearInterval(state.timer);
}

function bindEvents() {
  bindProductionEvents();
  window.addEventListener("resize", () => { if (state.currentView === "timeline") renderTimeline(); });
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  document.getElementById("project-switcher").addEventListener("click", async () => {
    const title = prompt("Nombre del proyecto:", state.project.title);
    if (title === null || !title.trim()) return;
    try {
      const payload = await api("/projects/1", { method: "PUT", body: JSON.stringify({ title: title.trim(), aspect_ratio: state.project.aspect_ratio }) });
      updateProjectFromPayload(payload);
      toast("Nombre de proyecto guardado.", "success");
    } catch (error) { toast(error.message, "error"); }
  });
  document.getElementById("save-scene").addEventListener("click", saveScene);
  document.getElementById("lock-scene").addEventListener("click", async () => {
    const scene = selectedScene();
    if (!scene) return;
    scene.locked = !scene.locked;
    await saveScene();
  });
  document.getElementById("upload-resource").addEventListener("click", () => document.getElementById("image-file").click());
  document.getElementById("change-resource").addEventListener("click", () => document.getElementById("image-file").click());
  document.getElementById("image-file").addEventListener("change", (event) => uploadImage(event.target.files[0]));
  document.getElementById("library-upload").addEventListener("click", () => document.getElementById("library-file").click());
  document.getElementById("library-file").addEventListener("change", (event) => uploadImage(event.target.files[0]));
  document.getElementById("video-stage").addEventListener("dragover", (event) => { event.preventDefault(); event.currentTarget.classList.add("drag-hover"); });
  document.getElementById("video-stage").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("drag-hover"));
  document.getElementById("video-stage").addEventListener("drop", (event) => { event.preventDefault(); event.currentTarget.classList.remove("drag-hover"); uploadImage(event.dataTransfer.files[0]); });
  document.querySelectorAll("[data-inspector-tab]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-inspector-tab]").forEach((tab) => tab.classList.toggle("active", tab === button));
    document.getElementById("scene-inspector").classList.toggle("hidden", button.dataset.inspectorTab !== "scene");
    document.getElementById("library-inspector").classList.toggle("hidden", button.dataset.inspectorTab !== "library");
  }));
  document.getElementById("play-button").addEventListener("click", () => togglePlayback());
  const audio = document.getElementById("timeline-audio");
  audio.addEventListener("timeupdate", () => { if (state.currentView === "timeline" && audio.hasAttribute("src")) setCurrentTime(audio.currentTime, false); });
  audio.addEventListener("play", () => { document.getElementById("play-button").textContent = "Ⅱ"; });
  audio.addEventListener("pause", () => { document.getElementById("play-button").textContent = "▶"; });
  document.getElementById("review-play").addEventListener("click", () => togglePlayback("review-play"));
  const reviewVideo = document.getElementById("review-video");
  reviewVideo.addEventListener("timeupdate", () => {
    state.currentTime = reviewVideo.currentTime;
    const active = getSceneAtTime(state.currentTime);
    if (active && active.id !== state.selectedSceneId) { state.selectedSceneId = active.id; renderInspector(); }
    document.getElementById("review-timecode").textContent = `${formatTime(reviewVideo.currentTime)} / ${formatTime(reviewVideo.duration || state.project?.duration_seconds || 0)}`;
    document.getElementById("composer-timestamp").textContent = formatTime(state.currentTime, 2);
    if (active) document.getElementById("composer-scene").textContent = `· Escena ${String(active.order_index).padStart(2, "0")}`;
  });
  reviewVideo.addEventListener("play", () => { state.playing = true; document.getElementById("review-play").textContent = "Ⅱ"; document.getElementById("play-button").textContent = "Ⅱ"; });
  reviewVideo.addEventListener("pause", () => { state.playing = false; document.getElementById("review-play").textContent = "▶"; document.getElementById("play-button").textContent = "▶"; });
  document.getElementById("back-five").addEventListener("click", () => setCurrentTime(state.currentTime - 5));
  document.getElementById("forward-five").addEventListener("click", () => setCurrentTime(state.currentTime + 5));
  document.getElementById("previous-scene").addEventListener("click", () => { const index = state.scenes.findIndex((scene) => scene.id === state.selectedSceneId); chooseScene(state.scenes[Math.max(0, index - 1)].id, true); });
  document.getElementById("next-scene").addEventListener("click", () => { const index = state.scenes.findIndex((scene) => scene.id === state.selectedSceneId); chooseScene(state.scenes[Math.min(state.scenes.length - 1, index + 1)].id, true); });
  document.getElementById("fullscreen-button").addEventListener("click", () => document.getElementById("video-stage").requestFullscreen?.());
  document.getElementById("volume-button").addEventListener("click", () => setView("audio"));
  document.getElementById("split-scene").addEventListener("click", () => mutateScene("split"));
  document.getElementById("merge-scene").addEventListener("click", () => mutateScene("merge"));
  document.getElementById("delete-scene").addEventListener("click", () => mutateScene("delete"));
  document.getElementById("add-scene").addEventListener("click", () => mutateScene("add"));
  document.getElementById("regenerate-image").addEventListener("click", () => generateImages(false, selectedScene()?.id));
  document.getElementById("regenerate-from-here").addEventListener("click", () => { setView("export"); generateImages(false, selectedScene()?.id); });
  document.getElementById("zoom-range").addEventListener("input", (event) => {
    state.timelineZoom = Number(event.target.value);
    document.getElementById("timeline-content").style.zoom = String(state.timelineZoom / 100);
  });
  document.getElementById("zoom-out").addEventListener("click", () => { const zoom = document.getElementById("zoom-range"); zoom.value = Math.max(Number(zoom.min), Number(zoom.value) - 10); zoom.dispatchEvent(new Event("input")); });
  document.getElementById("zoom-in").addEventListener("click", () => { const zoom = document.getElementById("zoom-range"); zoom.value = Math.min(Number(zoom.max), Number(zoom.value) + 10); zoom.dispatchEvent(new Event("input")); });
  document.getElementById("fit-timeline").addEventListener("click", () => { const zoom = document.getElementById("zoom-range"); zoom.value = 100; zoom.dispatchEvent(new Event("input")); document.getElementById("timeline-scroller").scrollLeft = 0; });
  document.getElementById("script-provider").addEventListener("change", syncScriptProvider);
  document.getElementById("generate-script").addEventListener("click", generateScript);
  document.getElementById("copy-script").addEventListener("click", async () => {
    const text = document.getElementById("script-output").value;
    if (!text) return toast("Todavía no hay un guion para copiar.");
    await navigator.clipboard.writeText(text);
    toast("Guion copiado al portapapeles.", "success");
  });
  document.getElementById("comment-this").addEventListener("click", () => {
    const reviewVideo = document.getElementById("review-video");
    if (!reviewVideo.classList.contains("hidden")) {
      reviewVideo.pause();
      state.currentTime = reviewVideo.currentTime;
    }
    if (state.playing) {
      state.playing = false;
      clearInterval(state.timer);
      document.getElementById("play-button").textContent = "▶";
      document.getElementById("review-play").textContent = "▶";
    }
    document.getElementById("review-note-input").focus();
    toast(`Nota para ${formatTime(state.currentTime)} · ${selectedScene() ? `Escena ${selectedScene().order_index}` : "proyecto"}.`);
  });
  document.getElementById("save-review-note").addEventListener("click", saveReviewNote);
  document.getElementById("apply-notes").addEventListener("click", applyReviewNotes);
  document.getElementById("generate-tts").addEventListener("click", () => generateTts());
  document.getElementById("generate-pending-voices").addEventListener("click", () => generateTts({ pendingOnly: true }));
  document.getElementById("master-audio-file").addEventListener("change", () => uploadAudioAsset("master-audio-file", "/audio/master", "Audio maestro cargado."));
  document.getElementById("music-file").addEventListener("change", () => uploadAudioAsset("music-file", "/audio/music", "Música guardada para el render."));
  document.getElementById("logo-file").addEventListener("change", () => uploadAudioAsset("logo-file", "/audio/logo", "Logotipo guardado para el render."));
  document.getElementById("sync-master-audio").addEventListener("click", async () => {
    try { const { job_id } = await api("/audio/sync", { method: "POST", body: "{}" }); await waitForJob(job_id, "sync-job-status"); await reloadProject(); toast("Tiempos sincronizados con el audio maestro.", "success"); }
    catch (error) { toast(error.message, "error"); }
  });
  document.getElementById("upload-sfx").addEventListener("click", addSoundEffect);
  document.getElementById("save-audio-settings").addEventListener("click", saveAudioSettings);
  ["voice-gain", "music-gain", "sfx-gain", "music-ducking", "logo-position", "logo-scale", "logo-opacity", "cartesia-rate"].forEach((id) => {
    document.getElementById(id).addEventListener("change", saveAudioSettings);
  });
  document.getElementById("tts-provider").addEventListener("change", (event) => {
    clearGeminiSample();
    document.getElementById("tts-model").value = "";
    if (event.target.value === "edge") {
      document.getElementById("tts-voice").value = "es-CO-SalomeNeural";
    } else {
      document.getElementById("tts-voice").value = "";
      document.getElementById("tts-voice").placeholder = event.target.value === "openai" ? "coral, marin, cedar…" : "Pega el ID de voz desde el catálogo del proveedor";
    }
    renderGeminiSettings();
    persistVoiceSettings();
  });
  ["tts-voice", "tts-model"].forEach((id) => document.getElementById(id).addEventListener("change", persistVoiceSettings));
  document.getElementById("gemini-voice").addEventListener("change", () => { clearGeminiSample(); persistVoiceSettings(); });
  document.getElementById("gemini-tts-tier").addEventListener("change", () => { renderGeminiSettings(); persistVoiceSettings(); });
  document.getElementById("gemini-voice-sample").addEventListener("click", playGeminiSample);
  ["render-aspect", "subtitle-mode"].forEach((id) => document.getElementById(id).addEventListener("change", async () => {
    const settings = renderSettings();
    try {
      await api("/render/settings", { method: "PUT", body: JSON.stringify(settings) });
      Object.assign(state.project, settings);
    } catch (error) { toast(error.message, "error"); }
  }));
  document.getElementById("image-provider").addEventListener("change", (event) => {
    document.getElementById("image-model").value = event.target.value === "openai" ? "gpt-image-2.5-flare" : "black-forest-labs/flux-schnell";
  });
  document.getElementById("generate-missing-images").addEventListener("click", () => generateImages(true));
  document.getElementById("generate-all-images").addEventListener("click", () => generateImages(false));
  document.getElementById("render-video").addEventListener("click", renderVideo);
  document.getElementById("refresh-providers").addEventListener("click", loadProviders);
  document.getElementById("script-model").addEventListener("input", () => {});
}

async function start() {
  bindEvents();
  try {
    await reloadProject();
    await loadProviders();
  } catch (error) { toast(`No se pudo cargar el proyecto local: ${error.message}`, "error"); }
}

start();
