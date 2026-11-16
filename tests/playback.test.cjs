const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('douyin-mcp/ui.html', 'utf8');
const source = html.slice(html.indexOf('async function recoverDouyinPlayback()'), html.indexOf("playerVideo.addEventListener('error'"));
function element() {
  const classes = new Set();
  return {classList: {add: x => classes.add(x), remove: x => classes.delete(x)}, classes};
}
function context(fetch) {
  const ctx = {fetch, currentVideo: {id: 'first'}, currentPlatform: 'douyin', playbackGeneration: 1,
    playerRefreshBtn: {}, playerVideo: element(), playerFallback: element(), playerFallbackMsg: {},
    playbackRecoveryAttempted: false, DEFAULT_PLAYBACK_RATE: 1.75};
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx;
}
(async () => {
  const success = context(async () => ({ok: true, json: async () => ({ok: true})}));
  await success.recoverDouyinPlayback();
  assert.match(success.playerVideo.src, /first\?retry=/);
  assert.equal(success.playerRefreshBtn.disabled, false);
  assert.equal(success.playbackRecoveryAttempted, true);

  let resolve;
  const stale = context(() => new Promise(r => {resolve = r;}));
  const pending = stale.recoverDouyinPlayback();
  stale.playbackGeneration++;
  stale.currentVideo = {id: 'second'};
  stale.playerVideo.src = 'second';
  resolve({ok: true, json: async () => ({ok: true})});
  await pending;
  assert.equal(stale.playerVideo.src, 'second');

  const failed = context(async () => {throw new Error('offline');});
  await failed.recoverDouyinPlayback();
  assert.match(failed.playerFallbackMsg.textContent, /offline/);
  assert.equal(failed.playerRefreshBtn.disabled, false);
  assert.equal(failed.playerFallback.classes.has('show'), true);

  const markSource = html.slice(html.indexOf('function markPlayingVideoWatched()'), html.indexOf('function promptPlaybackTags()'));
  let calls = 0;
  const watched = {currentVideo: {id: 'v', watched: false}, toggleWatched: async () => {calls++;}};
  vm.createContext(watched); vm.runInContext(markSource, watched);
  watched.markPlayingVideoWatched(); watched.markPlayingVideoWatched();
  assert.equal(calls, 1);
  // Reproduce the reported failure: a video can play without sending messages.
  // Advancing the watchdog must leave the media and capture layer untouched.
  const timerSource = html.slice(html.indexOf('function armYtFallbackTimer()'), html.indexOf("playerFrame.addEventListener('load'"));
  let watchdog;
  const timer = {clearTimeout() {}, setTimeout(fn) {watchdog = fn; return 1;},
    ytFallbackTimer: null, playerYtNotice: {textContent: ''}, playbackGeneration: 1,
    currentPlatform: 'youtube', currentVideo: {id: 'playing'}, YT_LOAD_TIMEOUT_MS: 9000,
    playerFrame: element(), playerYtCapture: element(), playerFallback: element()};
  vm.createContext(timer); vm.runInContext(timerSource, timer);
  timer.armYtFallbackTimer(); watchdog();
  assert.equal(timer.playerFrame.classes.has('hidden'), false);
  assert.equal(timer.playerYtCapture.classes.has('hidden'), false);
  assert.equal(timer.playerFallback.classes.has('show'), false);
  assert.match(timer.playerYtNotice.textContent, /creator name/);
  timer.armYtFallbackTimer(); timer.playbackGeneration++; watchdog();
  assert.equal(timer.playerYtNotice.textContent, '');

  // Explicit YouTube errors still surface; unrelated frames cannot affect playback.
  const messageSource = html.slice(html.indexOf("window.addEventListener('message',"), html.indexOf("playerRefreshBtn.addEventListener('click'"));
  let messageHandler;
  timer.window = {addEventListener(name, callback) {messageHandler = callback;}};
  timer.playerFrame.contentWindow = {};
  timer.playerFallbackMsg = {};
  timer.playerRefreshBtn = {};
  timer.markPlayingVideoWatched = () => {};
  vm.runInContext(messageSource, timer);
  messageHandler({origin: 'https://www.youtube.com', source: {}, data: JSON.stringify({event: 'onError', info: 150})});
  assert.equal(timer.playerFrame.classes.has('hidden'), false);
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'onError', info: 150})});
  assert.equal(timer.playerFrame.classes.has('hidden'), true);
  assert.equal(timer.playerFallback.classes.has('show'), true);
  assert.match(timer.playerFallbackMsg.textContent, /150.*owner does not allow/);
  assert.equal(timer.playerRefreshBtn.hidden, true);
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'onError', info: 101})});
  assert.equal(timer.playerRefreshBtn.hidden, true);
  assert.match(timer.playerFallbackMsg.textContent, /return here to rate and tag/);
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'onError', info: 153})});
  assert.match(timer.playerFallbackMsg.textContent, /153.*referrer/);
  assert.equal(timer.playerRefreshBtn.hidden, false);
  timer.playerFrame.classList.remove('hidden');
  timer.playerFallback.classList.remove('show');
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'onAutoplayBlocked'})});
  assert.equal(timer.playerYtCapture.classes.has('hidden'), true);
  assert.equal(timer.playerFrame.classes.has('hidden'), false);
  assert.equal(timer.playerFallback.classes.has('show'), false);
  assert.match(timer.playerYtNotice.textContent, /Tap Play/);
  assert.match(html, /referrerpolicy="strict-origin-when-cross-origin"/);
  assert.equal((html.match(/autoplay=1&playsinline=1/g) || []).length, 2);
  let sheetOpens = 0, pauses = 0, navigations = 0;
  const handlers = {};
  const controls = {currentVideo: {id: 'v'}, currentPlatform: 'douyin',
    playerVideo: {pause() {pauses++;}, addEventListener(name, fn) {handlers[name] = fn;}},
    openSheet() {sheetOpens++;}, ytCommand(command) {assert.equal(command, 'pauseVideo');}};
  vm.createContext(controls);
  vm.runInContext(html.slice(html.indexOf('function promptPlaybackTags()'), html.indexOf("playerVideo.addEventListener('playing'")), controls);
  handlers.ended();
  assert.equal(sheetOpens, 1);
  assert.equal(pauses, 1);
  controls.currentPlatform = 'youtube';
  controls.promptPlaybackTags();
  assert.equal(controls.ytPlaying, false);
  assert.equal(sheetOpens, 2);
  controls.currentVideo = null;
  controls.promptPlaybackTags();
  assert.equal(sheetOpens, 2);
  timer.promptPlaybackTags = () => {sheetOpens++;};
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'onStateChange', info: 0})});
  assert.equal(sheetOpens, 3);

  let keyHandler;
  const keyboard = {window: {}, document: {activeElement: {tagName: 'BODY'}, addEventListener(name, fn) {keyHandler = fn;}},
    playerOverlay: {classList: {contains: () => true}},
    playerSheet: {classList: {contains: () => false}},
    goToVideo(delta) {assert.equal(delta, 1); navigations++;}};
  vm.createContext(keyboard);
  vm.runInContext(html.slice(html.indexOf("document.addEventListener('keydown',"), html.indexOf('const PLAY_ICON')), keyboard);
  const down = {key: 'ArrowDown', preventDefault() {}};
  keyHandler(down);
  assert.equal(navigations, 1);
  keyHandler({...down, repeat: true});
  assert.equal(navigations, 1);
  for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) {
    keyboard.document.activeElement = {tagName};
    keyHandler(down);
  }
  keyboard.document.activeElement = {isContentEditable: true};
  keyHandler(down);
  assert.equal(navigations, 1);
  assert.doesNotMatch(html, /playsinline loop|&loop=1/);
  assert.doesNotMatch(html, /id="playerProgressTrack"|id="playerSeek"/);
  assert.equal((html.match(/controls=1&enablejsapi=1/g) || []).length, 2);
  console.log('Playback: recovery, stale responses, watched state, non-disruptive timeout, and explicit YouTube errors passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
