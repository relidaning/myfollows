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

  const markSource = html.slice(html.indexOf('function markPlayingVideoWatched()'), html.indexOf("playerVideo.addEventListener('playing'"));
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
  timer.markPlayingVideoWatched = () => {};
  vm.runInContext(messageSource, timer);
  messageHandler({origin: 'https://www.youtube.com', source: {}, data: JSON.stringify({event: 'onError', info: 150})});
  assert.equal(timer.playerFrame.classes.has('hidden'), false);
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'onError', info: 150})});
  assert.equal(timer.playerFrame.classes.has('hidden'), true);
  assert.equal(timer.playerFallback.classes.has('show'), true);
  assert.match(timer.playerFallbackMsg.textContent, /150.*owner does not allow/);
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'onError', info: 153})});
  assert.match(timer.playerFallbackMsg.textContent, /153.*referrer/);
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
  const nodes = Object.fromEntries(['playerSeek', 'playerElapsed', 'playerDuration'].map(id =>
    [id, {setAttribute(name, value) {this[name] = value;}}]));
  const progress = {Number, Math, String, playerProgressFill: {style: {}}, document: {getElementById: id => nodes[id]}};
  vm.createContext(progress);
  vm.runInContext(html.slice(html.indexOf('let playbackDuration = 0;'), html.indexOf("for (const event of ['timeupdate'")), progress);
  progress.updatePlaybackProgress(75, 300);
  assert.equal(progress.playerProgressFill.style.width, '25%');
  assert.equal(nodes.playerSeek.value, 250);
  assert.equal(nodes.playerElapsed.textContent, '1:15');
  assert.equal(nodes.playerDuration.textContent, '5:00');
  progress.updatePlaybackProgress(500, 300);
  assert.equal(nodes.playerSeek.value, 1000);
  progress.updatePlaybackProgress(0, NaN);
  assert.equal(nodes.playerSeek.disabled, true);
  assert.equal(nodes.playerDuration.textContent, '--:--');
  timer.updatePlaybackProgress = (time, duration) => { timer.progress = [time, duration]; };
  messageHandler({origin: 'https://www.youtube.com', source: timer.playerFrame.contentWindow,
    data: JSON.stringify({event: 'infoDelivery', info: {currentTime: 45, duration: 180}})});
  assert.deepEqual(timer.progress, [45, 180]);
  console.log('Playback: recovery, stale responses, watched state, non-disruptive timeout, and explicit YouTube errors passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
