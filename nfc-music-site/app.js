const elements = {
  player: document.querySelector(".player"),
  cover: document.querySelector("#cover"),
  edition: document.querySelector("#edition"),
  mood: document.querySelector("#mood"),
  title: document.querySelector("#title"),
  artist: document.querySelector("#artist"),
  description: document.querySelector("#description"),
  duration: document.querySelector("#duration"),
  currentTime: document.querySelector("#currentTime"),
  playButton: document.querySelector("#playButton"),
  playIcon: document.querySelector("#playIcon"),
  seek: document.querySelector("#seek"),
  audio: document.querySelector("#audio"),
};

let cards = {};
let currentCard = null;
let synth = null;
let synthStartedAt = 0;
let synthOffset = 0;
let rafId = 0;

init();

async function init() {
  try {
    const response = await fetch("/data/cards.json", { cache: "no-store" });
    cards = await response.json();
    const cardId = getCardId();
    currentCard = cards[cardId] ?? cards.card001;
    renderCard(currentCard, cardId);
    bindPlayer();
  } catch (error) {
    renderError();
    console.error(error);
  }
}

function getCardId() {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get("card");
  if (fromQuery) return fromQuery;

  const parts = window.location.pathname.split("/").filter(Boolean);
  const cardIndex = parts.indexOf("c");
  return cardIndex >= 0 ? parts[cardIndex + 1] : "card001";
}

function renderCard(card, requestedId) {
  document.title = `${card.title} | NFC Music`;
  document.documentElement.style.setProperty("--accent", card.accent);
  document.documentElement.style.setProperty("--secondary", card.secondary);

  elements.edition.textContent = card.edition;
  elements.mood.textContent = card.mood;
  elements.title.textContent = card.title;
  elements.artist.textContent = card.artist;
  elements.description.textContent = card.description;
  elements.duration.textContent = card.duration;

  if (card.coverUrl) {
    elements.cover.classList.add("has-cover");
    elements.cover.style.backgroundImage = `url("${card.coverUrl}")`;
  }

  if (card.audioUrl) {
    elements.audio.src = card.audioUrl;
  }

  document.querySelectorAll(".cards a").forEach((link) => {
    const url = new URL(link.href);
    if (url.searchParams.get("card") === requestedId) {
      link.setAttribute("aria-current", "page");
    }
  });
}

function renderError() {
  elements.title.textContent = "No Signal";
  elements.artist.textContent = "Card data was not found";
  elements.description.textContent = "Check the card URL or data/cards.json.";
}

function bindPlayer() {
  elements.playButton.addEventListener("click", togglePlayback);

  elements.audio.addEventListener("loadedmetadata", () => {
    elements.duration.textContent = formatTime(elements.audio.duration);
  });

  elements.audio.addEventListener("timeupdate", () => {
    if (!elements.audio.duration) return;
    elements.currentTime.textContent = formatTime(elements.audio.currentTime);
    elements.seek.value = String(
      Math.round((elements.audio.currentTime / elements.audio.duration) * 1000),
    );
  });

  elements.audio.addEventListener("ended", () => stopPlayback(true));

  elements.seek.addEventListener("input", () => {
    if (currentCard.audioUrl && elements.audio.duration) {
      elements.audio.currentTime =
        (Number(elements.seek.value) / 1000) * elements.audio.duration;
      return;
    }

    synthOffset = (Number(elements.seek.value) / 1000) * durationToSeconds(currentCard.duration);
    if (synth) {
      synthStartedAt = performance.now() / 1000 - synthOffset;
    }
    updateSynthTime();
  });
}

async function togglePlayback() {
  if (elements.player.classList.contains("is-playing")) {
    pausePlayback();
    return;
  }

  if (currentCard.audioUrl) {
    await elements.audio.play();
    startUi();
    return;
  }

  startSynth();
  startUi();
  animateSynthTime();
}

function startUi() {
  elements.player.classList.add("is-playing");
  elements.playIcon.textContent = "Pause";
  elements.playButton.setAttribute("aria-label", "Pause");
}

function pausePlayback() {
  if (currentCard.audioUrl) {
    elements.audio.pause();
  } else {
    synthOffset = performance.now() / 1000 - synthStartedAt;
    stopSynth();
  }

  elements.player.classList.remove("is-playing");
  elements.playIcon.textContent = "Play";
  elements.playButton.setAttribute("aria-label", "Play");
  cancelAnimationFrame(rafId);
}

function stopPlayback(reset = false) {
  if (currentCard.audioUrl) {
    elements.audio.pause();
    if (reset) elements.audio.currentTime = 0;
  } else {
    if (!reset) {
      synthOffset = performance.now() / 1000 - synthStartedAt;
    }
    stopSynth();
    if (reset) synthOffset = 0;
  }

  elements.player.classList.remove("is-playing");
  elements.playIcon.textContent = "Play";
  elements.playButton.setAttribute("aria-label", "Play");
  cancelAnimationFrame(rafId);

  if (reset) {
    elements.seek.value = "0";
    elements.currentTime.textContent = "0:00";
  }
}

function startSynth() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  const context = new AudioContext();
  const master = context.createGain();
  const filter = context.createBiquadFilter();
  const oscA = context.createOscillator();
  const oscB = context.createOscillator();

  filter.type = "lowpass";
  filter.frequency.value = 720;
  master.gain.value = 0.08;
  oscA.frequency.value = 110;
  oscB.frequency.value = 165;

  oscA.connect(filter);
  oscB.connect(filter);
  filter.connect(master);
  master.connect(context.destination);
  oscA.start();
  oscB.start();

  synth = { context, master, oscA, oscB };
  synthStartedAt = performance.now() / 1000 - synthOffset;
}

function stopSynth() {
  if (!synth) return;
  synth.master.gain.setTargetAtTime(0, synth.context.currentTime, 0.03);
  window.setTimeout(() => synth?.context.close(), 120);
  synth = null;
}

function animateSynthTime() {
  updateSynthTime();
  rafId = requestAnimationFrame(animateSynthTime);
}

function updateSynthTime() {
  const total = durationToSeconds(currentCard.duration);
  const elapsed = synth
    ? performance.now() / 1000 - synthStartedAt
    : synthOffset;

  if (elapsed >= total) {
    stopPlayback(true);
    return;
  }

  elements.currentTime.textContent = formatTime(elapsed);
  elements.seek.value = String(Math.round((elapsed / total) * 1000));
}

function durationToSeconds(duration) {
  const [minutes, seconds] = duration.split(":").map(Number);
  return minutes * 60 + seconds;
}

function formatTime(value) {
  if (!Number.isFinite(value)) return "0:00";
  const minutes = Math.floor(value / 60);
  const seconds = Math.floor(value % 60);
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
