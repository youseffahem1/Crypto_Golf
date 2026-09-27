/* Drives the real "Move to Wallet" flow out of frontend/index.html: PROFIT is
   rendered, the source of the profit is listed, the button opens the dialog, a
   coin is chosen, a PIN is typed, and only then is a request made. The code
   under test is sliced straight out of the file, so this cannot drift from the
   implementation.

   What is stubbed is the SERVER, not the page: /api/platform/coins and
   /api/trade/history answer with the same shapes the backend returns (verified
   by backend/smoke_profit_pin.py), and /api/platform/move-profit records the
   request instead of moving money. That lets this test prove the page sends the
   right thing and refuses a wrong PIN without a live account.

   Run: node test_profit_move_flow.js <path-to-index.html>                     */
const fs = require('fs');
const vm = require('vm');

const file = process.argv[2];
const html = fs.readFileSync(file, 'utf8');
const lines = html.split(/\r?\n/);

function slice(fromMarker, toMarker) {
  const a = lines.findIndex(l => l.includes(fromMarker));
  if (a < 0) throw new Error('start marker not found: ' + fromMarker);
  let b = -1;
  for (let i = a + 1; i < lines.length; i++) {
    if (toMarker && lines[i].includes(toMarker)) { b = i; break; }
  }
  if (b < 0) throw new Error('end marker not found: ' + toMarker);
  return lines.slice(a, b).join('\n');
}

/* The dashboard-cards block: mainEl, byId, usd, setText, setNeg, activeSymbol,
   liveSplit, setMoveAvailability, vantaApplyMovedProfit and writeResult. */
const renderSrc = slice('if (window.__vantaDashboardCards) return;', 'function refreshInfoCards(){');
/* The two-step Move dialog, its helpers and its wiring. */
const moveSrc = slice('"Move to Wallet": realized trading profit',
  '/* ---- Dashboard balance card quick actions (delegated) ---- */');
/* The cards' button wiring. It lives below refreshInfoCards, so it needs its
   own slice. Started a little earlier than `wire()` itself so the scroll
   helper it calls is in scope. */
const wireSrc = slice('function scrollToTrade(){', 'setInterval(function(){');

const FAILS = [];
function check(label, got, want) {
  if (got !== want) { FAILS.push(label + ': got ' + got + ' want ' + want); console.log('  FAIL ' + label + ': got ' + got + ' want ' + want); }
  else console.log('  ok   ' + label + ' = ' + got);
}
function ok(label, cond) { check(label, !!cond, true); }

/* ---- a DOM stub with just enough behaviour for the real code ------------- */
function El(id) {
  const handlers = {};
  return {
    id, textContent: '', innerHTML: '', value: '', className: '', hidden: false,
    disabled: false, title: '', type: '', maxlength: '', placeholder: '',
    dataset: {}, attrs: {}, classes: new Set(), coins: [],
    classList: {
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { const want = on === undefined ? !this._s.has(c) : !!on; if (want) this._s.add(c); else this._s.delete(c); },
      _s: null,
    },
    addEventListener(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    querySelectorAll(sel) {
      if (sel === '.vpm-coin') return this.coins;
      return [];
    },
    querySelector() { return null; },
    closest() { return null; },
    focus() {},
    blur() {},
    _fire(ev, e) { (handlers[ev] || []).forEach(fn => fn(e || { target: this, preventDefault() {} })); },
    _has(ev) { return (handlers[ev] || []).length > 0; },
  };
}

const els = {};
for (const id of ['icProfit', 'icLoss', 'icProfitSub', 'icMoveBtn',
  'vantaProfitMove', 'vpmTitle', 'vpmSub', 'vpmStep1', 'vpmStep2', 'vpmCoinStep',
  'vpmCoins', 'vpmSrc', 'vpmAmount', 'vpmTo', 'vpmNext', 'vpmCancel']) els[id] = El(id);
/* classList needs its backing set bound to the element it belongs to. */
for (const id in els) els[id].classList._s = els[id].classes;

const DOTS = [El('d0'), El('d1'), El('d2'), El('d3'), El('d4')];
DOTS.forEach(d => { d.classList._s = d.classes; });
const COINS = [
  { symbol: 'USDT', name: 'Tether', price: 1, tradeable: true },
  { symbol: 'GOLF', name: 'Golf Coin', price: 2, tradeable: true },
  { symbol: 'BTC', name: 'Bitcoin', price: 50000, tradeable: true },
  { symbol: 'DOGE2', name: 'Untradeable', price: 0, tradeable: false },
];
const HISTORY = [
  { id: 't-1', symbol: 'GOLF', direction: 'UP', amount: 100, profit: 20, status: 'WON', settled_at: '2026-09-27T10:00:00Z', profit_moved: false },
  { id: 't-2', symbol: 'GOLF', direction: 'UP', amount: 50, profit: 5, status: 'WON', settled_at: '2026-09-27T11:00:00Z', profit_moved: true },
  { id: 't-3', symbol: 'GOLF', direction: 'DOWN', amount: 50, profit: -10, status: 'LOST', settled_at: '2026-09-27T12:00:00Z' },
  { id: 't-4', symbol: 'BTC', direction: 'UP', amount: 1, profit: 99, status: 'WON', settled_at: '2026-09-27T13:00:00Z', profit_moved: false },
];
const TOASTS = [];
const REQUESTS = [];
let PIN_STATE = { has_pin: true };
/* The move endpoint's answer, and the failure it should give for a bad PIN. */
let MOVE_BEHAVIOUR = { status: 200, body: { symbol: 'GOLF', dest_symbol: 'GOLF', usd_moved: 20, coin_amount: 10, price: 2 } };

function apiStub(path, opts) {
  REQUESTS.push({ path, method: (opts && opts.method) || 'GET', body: opts && opts.body });
  if (path === '/api/platform/coins') return Promise.resolve({ coins: COINS });
  if (path === '/api/trade/history') return Promise.resolve(HISTORY);
  if (path === '/api/wallet/pin') {
    if (opts && opts.method === 'POST') { PIN_STATE = { has_pin: true }; return Promise.resolve({ ok: true, has_pin: true }); }
    return Promise.resolve(PIN_STATE);
  }
  if (path === '/api/platform/move-profit') {
    if (MOVE_BEHAVIOUR.status !== 200) {
      return Promise.reject(new Error(MOVE_BEHAVIOUR.detail));
    }
    return Promise.resolve(MOVE_BEHAVIOUR.body);
  }
  return Promise.resolve(null);
}
const tick = () => new Promise(r => setImmediate(r));

/* The shared Wallet PIN gate, stubbed the way the page really runs it.
   The Move dialog has no PIN field of its own any more: it calls
   vantaRequirePin(action, opts), and on the FIRST use that gate CREATES whatever
   4–5 digits the client typed, on every use after it VERIFIES the same one, and
   only then hands those digits to the action. So `typed` is what the user types
   into the one prompt, and `stored` is the account's PIN. */
const PIN_GATE = {
  stored: '4321', typed: '4321', open: false, opts: null, _action: null,
  ask(action, opts) { this._action = action; this.opts = opts || null; this.open = true; },
  /* The user pressed the shared dialog's button. */
  submit() {
    const pin = this.typed;
    REQUESTS.push(this.stored == null
      ? { path: '/api/wallet/pin', method: 'POST', body: { pin } }
      : { path: '/api/wallet/pin/verify', method: 'POST', body: { pin } });
    this.stored = pin;
    this.open = false;
    this._action(pin);
  },
  /* The user closed the shared dialog without answering it: no request at all. */
  cancel() {
    this.open = false;
    if (this.opts && typeof this.opts.onCancel === 'function') this.opts.onCancel();
  },
};

const sandbox = {
  Math, Number, String, Array, Object, JSON, Boolean, isFinite, Date, Promise, RegExp, Error, parseInt, parseFloat,
  setTimeout: (fn) => { try { fn(); } catch (e) { /* page timers are not under test */ } },
  clearTimeout() {},
  requestAnimationFrame: (fn) => { if (typeof fn === 'function') fn(); return 0; },
  console,
  document: {
    getElementById: id => els[id] || null,
    querySelector: () => null,
    querySelectorAll: sel => (sel === '#vpmDots .vpin-dot' ? DOTS : []),
    addEventListener() {},
  },
  localStorage: { _d: {}, getItem(k) { return this._d[k] === undefined ? null : this._d[k]; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } },
  accountMode: 'live',
  vantaApi: apiStub,
  /* The real page exposes this from its PIN dialog script. The Move dialog is
     required to route through it, so it is stubbed with the same contract. */
  vantaRequirePin: (action, opts) => PIN_GATE.ask(action, opts),
  vantaToken: () => 'test-token',
  vantaPlatformPrice: (s) => (COINS.find(c => c.symbol === s && c.tradeable) || { price: 0 }).price,
  vantaActiveCoin: 'GOLF',
  esc: s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  toast: m => TOASTS.push(m),
  showToast: m => TOASTS.push(m),
  /* UI refresh hooks: record that the page tried, without needing a real page. */
  vantaRefreshInfoCards: () => {},
  vantaRefreshWalletSplit: () => {},
  vantaRefreshDashboard: () => {},
  vantaSyncWalletUI: () => {},
  vantaSyncPosCard: () => {},
  updateBalance: () => {},
  refreshBalance: () => {},
};
sandbox.window = sandbox;
sandbox.window.vantaCoinBalances = null;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  'var __out={};\n(function(){\n' + renderSrc + '\n' + moveSrc + '\n' + wireSrc + '\n' +
  '  __out.writeResult=writeResult;__out.setMoveAvailability=setMoveAvailability;\n' +
  '  __out.dest=function(){return vpmDest;};__out.sym=function(){return vpmSym;};\n' +
  '  __out.open=window.vantaMoveProfitToWallet;return __out;\n})()',
  sandbox
);
const page = sandbox.__out || vm.runInContext('({open:window.vantaMoveProfitToWallet})', sandbox);
const fire = (id, ev, e) => els[id]._fire(ev, e);
const modalOpen = () => els.vantaProfitMove.classes.has('open');
const moveReqs = () => REQUESTS.filter(r => r.path === '/api/platform/move-profit');

(async function run() {

  console.log('[1] the card renders the PROFIT that is still movable, with no "+"');
  page.writeResult({ profit: 25, loss: -10, available: 20 }, 'GOLF');
  check('PROFIT is the amount the button will move, not the all-time total',
    els.icProfit.textContent, '$20.00');
  ok('and never a minus either', !/[-+]\$/.test(els.icProfit.textContent));
  check('LOSS still shows its own sign', els.icLoss.textContent, '\u2212$10.00');
  check('the subline names the amount that can move', /Move \$20\.00/.test(els.icProfitSub.textContent), true);
  await tick(); await tick();
  ok('the untradeable coin is not offered', !/DOGE2/.test(els.vpmCoins.innerHTML));

  console.log('\n[1a] once the profit is moved, THIS card reads $0.00');
  /* The client asked for the figure to zero after a transfer, and only on this
     card: the ledger keeps the all-time profit, the card follows the money. The
     server reports available_profit 0, so a reload lands on the same place. */
  page.writeResult({ profit: 25, loss: -10, available: 0 }, 'GOLF');
  check('PROFIT reads $0.00 after the move', els.icProfit.textContent, '$0.00');
  ok('LOSS is untouched by a move', els.icLoss.textContent === '\u2212$10.00');
  check('and the button is disabled with nothing left',
    els.icMoveBtn.disabled, true);
  check('the subline says the profit is already in the wallet',
    /already in your wallet/.test(els.icProfitSub.textContent), true);
  /* An account that never earned anything reads differently, so the two states
     can never be mistaken for each other. */
  page.writeResult({ profit: 0, loss: 0, available: 0 }, 'GOLF');
  check('a never-earned profit still reads as "not yet"',
    /No realized GOLF profit to move yet/.test(els.icProfitSub.textContent), true);
  page.writeResult({ profit: 25, loss: -10, available: 20 }, 'GOLF');
  check('PROFIT is back for the rest of the flow', els.icProfit.textContent, '$20.00');

  console.log('\n[1b] the per-trade source list is gone from the page for good');
  /* It was a disclosure under the PROFIT figure listing the closed trades behind
     it. The client asked for it to be deleted outright, so this asserts the
     absence in all three places it used to live: markup, styles, and logic. */
  check('no ic-src class in the markup', /class="[^"]*\bic-src\b/.test(html), false);
  check('no ic-src class in the styles', /\bic-src[\s{.:[]/.test(html), false);
  check('no icProfitSrc element', /id="icProfitSrc"/.test(html), false);
  check('no icProfitSrcList element', /id="icProfitSrcList"/.test(html), false);
  check('no renderer left behind', /renderProfitSource|refreshProfitSource/.test(html), false);
  check('no cache invalidator left behind', /icSrcInvalidate/.test(html), false);
  check('no source-row styles left behind', /ic-src-row|ic-src-more|ic-src-caret|ic-src-amt/.test(html), false);
  check('the subline that replaced it still renders', /id="icProfitSub"/.test(html), true);
  check('and the card still moves the money', /id="icMoveBtn"/.test(html), true);

  console.log('\n[2] clicking Move opens the dialog on the COIN step');
  check('the button is enabled', els.icMoveBtn.disabled, false);
  page.open();
  await tick(); await tick();
  check('the dialog is open', modalOpen(), true);
  check('the coin step is showing', els.vpmCoinStep.hidden, false);
  check('the next button says Continue', els.vpmNext.textContent, 'Continue');
  ok('real coins are offered', /BTC/.test(els.vpmCoins.innerHTML) && /GOLF/.test(els.vpmCoins.innerHTML));
  ok('untradeable coins are not offered', !/DOGE2/.test(els.vpmCoins.innerHTML));
  check('the amount is the available profit', els.vpmAmount.textContent, '$20.00');
  check('nothing has been sent yet', moveReqs().length, 0);
  check('and the PIN has not been asked for yet', PIN_GATE.open, false);

  console.log('\n[3] Continue hands over to the SHARED account PIN and sends nothing yet');
  const btc = El('btc'); btc.dataset.sym = 'BTC';
  els.vpmCoins.coins = [btc];
  fire('vpmCoins', 'click', { target: { closest: () => btc }, preventDefault() {} });
  fire('vpmNext', 'click');
  check('the chosen coin is the one the page will use', page.dest(), 'BTC');
  ok('the shared PIN prompt opened', PIN_GATE.open === true);
  check('and it was told the amount is already chosen', page.dest(), 'BTC');
  ok('the preview names the chosen coin', /BTC/.test(els.vpmTo.textContent));
  ok('and the price it used', /50,000|\$50000/.test(els.vpmTo.textContent));
  check('the coin step is still there to come back to', els.vpmCoinStep.hidden, false);
  check('the step pills say the PIN is up next', els.vpmStep2.className, 'vpm-step on');
  check('still nothing has been sent', moveReqs().length, 0);
  ok('the prompt offers to CREATE a PIN, not confirm one', /Create PIN & move/.test(PIN_GATE.opts.setupButton || ''));

  console.log('\n[4] the CORRECT PIN is what gets sent — the one the user typed');
  MOVE_BEHAVIOUR = { status: 200, body: { symbol: 'GOLF', dest_symbol: 'BTC', usd_moved: 20, coin_amount: 0.0004, price: 50000 } };
  PIN_GATE.typed = '4321';
  PIN_GATE.submit();
  await tick(); await tick();
  check('the existing PIN was verified, not re-created',
    REQUESTS.filter(r => r.path === '/api/wallet/pin/verify').length, 1);
  const req = moveReqs()[moveReqs().length - 1];
  check('it posted to the profit endpoint', req.path, '/api/platform/move-profit');
  check('it named the traded coin', req.body.symbol, 'GOLF');
  check('it named the CHOSEN destination', req.body.dest_symbol, 'BTC');
  check('it sent the PIN the user typed', req.body.pin, '4321');
  ok('it sent NO amount of its own', !('amount' in req.body) && !('usd_moved' in req.body));
  check('the dialog closed', modalOpen(), false);
  ok('the user is told what landed', /GOLF profit moved to your BTC wallet/.test(TOASTS[TOASTS.length - 1]));
  ok('and the real amount is quoted, not the estimate', /0\.0004/.test(TOASTS[TOASTS.length - 1]));

  console.log('\n[5] the PIN that Convert uses is the PIN that moves the profit');
  /* One account, one hash, one gate. A PIN created through the shared prompt is
     the same PIN this flow is asked for next time, because both go through
     vantaRequirePin -> /api/wallet/pin. */
  check('the flow has no PIN store of its own', /vpmPin\b/.test(moveSrc), false);
  check('and no PIN set call of its own', /api\/wallet\/pin/.test(moveSrc), false);
  check('it only ever asks through the shared gate', /window\.vantaRequirePin\(/.test(moveSrc), true);

  console.log('\n[5b] the PIN prompt is ON TOP of the dialog that opened it');
  /* The bug this guards: the PIN field rendered UNDER the Move dialog, so it was
     visible through the blur but could not be clicked or typed into. Every modal
     on the page is a fixed layer, so what decides this is z-index alone. */
  const layerOf = id => {
    const m = html.match(new RegExp('#' + id + '\\{[^}]*z-index:(\\d+)'));
    return m ? Number(m[1]) : null;
  };
  const zPin = layerOf('vantaPinModal'), zMove = layerOf('vantaProfitMove');
  ok('the PIN prompt has a z-index', zPin !== null);
  ok('the Move dialog has a z-index', zMove !== null);
  check('and the PIN prompt is above the Move dialog', zPin > zMove, true);
  /* Not just above this one: it is the last thing asked for on any protected
     action, so it has to beat every other overlay on the page. */
  const layers = [...html.matchAll(/z-index:(\d+)/g)].map(m => Number(m[1]));
  check('it is the topmost layer of all', zPin, Math.max(...layers));
  check('the shared prompt is still a real element', /id="vantaPinModal"/.test(html), true);

  console.log('\n[5c] dismissing the PIN prompt does not strand the Move dialog');
  els.icMoveBtn.dataset.vantaAvail = '20';
  page.open();
  await tick(); await tick();
  els.vpmCoins.coins = [btc];
  fire('vpmCoins', 'click', { target: { closest: () => btc }, preventDefault() {} });
  fire('vpmNext', 'click');
  check('the button waits on the PIN', els.vpmNext.textContent, 'Checking PIN…');
  check('and is disabled meanwhile', els.vpmNext.disabled, true);
  ok('the flow was told what to do on a cancel', typeof PIN_GATE.opts.onCancel === 'function');
  const sentBefore = moveReqs().length;
  PIN_GATE.cancel();
  check('the button is usable again', els.vpmNext.disabled, false);
  check('and says Continue, not "Checking PIN"', els.vpmNext.textContent, 'Continue');
  check('the coin choice was kept', page.dest(), 'BTC');
  check('and nothing was sent', moveReqs().length, sentBefore);
  check('the dialog is still open, on the coin step', els.vpmCoinStep.hidden, false);
  fire('vpmCancel', 'click');
  check('the dialog can be cancelled', modalOpen(), false);

  console.log('\n[6] a move the server refuses moves nothing and says why');
  els.icMoveBtn.dataset.vantaAvail = '20';
  TOASTS.length = 0;
  MOVE_BEHAVIOUR = { status: 400, detail: 'Invalid PIN.' };
  page.open();
  await tick(); await tick();
  PIN_GATE.typed = '9999';
  fire('vpmNext', 'click');
  const badReqs = moveReqs().length;
  PIN_GATE.submit();
  await tick(); await tick();
  check('a request was made and refused', moveReqs().length, badReqs + 1);
  check('no success message was shown', TOASTS.length, 1);
  check('the reason reached the user', /Invalid PIN/.test(TOASTS[TOASTS.length - 1]), true);
  check('the dialog stayed open to try again', modalOpen(), true);
  fire('vpmCancel', 'click');

  console.log('\n[6b] a lockout is reported as such, not as a wrong PIN');
  TOASTS.length = 0;
  MOVE_BEHAVIOUR = { status: 400, detail: 'Too many incorrect PIN attempts. Try again in 42s.' };
  fire('vpmNext', 'click');
  PIN_GATE.submit();
  await tick(); await tick();
  check('the lockout message reaches the user', /Too many/.test(TOASTS[TOASTS.length - 1]), true);
  check('the PIN is not described as merely wrong', /Incorrect PIN/.test(TOASTS[TOASTS.length - 1]), false);

  console.log('\n[7] the FIRST move: the client is asked to create a PIN, and it just moves');
  PIN_GATE.stored = null;               /* this account has no PIN yet */
  PIN_GATE.typed = '12345';             /* the client picks any 4–5 digits */
  els.icMoveBtn.dataset.vantaAvail = '20';
  TOASTS.length = 0;
  MOVE_BEHAVIOUR = { status: 200, body: { symbol: 'GOLF', dest_symbol: 'GOLF', usd_moved: 20, coin_amount: 10, price: 2, available_profit: 0 } };
  REQUESTS.length = 0;
  page.open();
  await tick(); await tick();
  fire('vpmNext', 'click');
  ok('the shared prompt is open', PIN_GATE.open === true);
  check('and the button says it will create and move', PIN_GATE.opts.setupButton, 'Create PIN & move');
  PIN_GATE.submit();
  await tick(); await tick();
  const pinAt = REQUESTS.findIndex(r => r.path === '/api/wallet/pin' && r.method === 'POST');
  const moveAt = REQUESTS.findIndex(r => r.path === '/api/platform/move-profit');
  ok('a PIN was stored before the move', pinAt > -1 && pinAt < moveAt);
  check('it stored exactly what the client typed', REQUESTS[pinAt].body.pin, '12345');
  check('then the profit moved', moveReqs().length, 1);
  check('carrying the same PIN', moveReqs()[0].body.pin, '12345');
  check('the dialog closed', modalOpen(), false);
  ok('and the client is told what landed', /GOLF profit moved to your GOLF wallet/.test(TOASTS[TOASTS.length - 1]));

  /* The client asked for this exactly: the moment the PIN is in and the money
     has moved, #icProfit reads $0.00. `vantaRefreshInfoCards` is a no-op stub in
     this sandbox, so nothing but the move response itself can have done it —
     the card is not waiting on a later poll. */
  check('#icProfit is $0.00 as soon as the move returns', els.icProfit.textContent, '$0.00');
  check('the Move button is off with nothing left', els.icMoveBtn.disabled, true);
  ok('and the sub-line says the profit is now in the wallet',
    /is now in your wallet/.test(els.icProfitSub.textContent));
  check('the amount it moved is named in that sub-line',
    /\$20\.00 of GOLF profit is now in your wallet/.test(els.icProfitSub.textContent), true);

  /* From now on the same PIN is asked for, exactly as Convert asks for it. */
  check('the account now has that PIN', PIN_GATE.stored, '12345');
  TOASTS.length = 0;
  page.open();
  await tick(); await tick();
  fire('vpmNext', 'click');
  check('a later move asks for the existing PIN instead', PIN_GATE.opts.loginButton, 'Move to wallet');
  /* Which of the two labels the user actually sees is the SHARED dialog's call,
     made from whether the account already has a PIN — not the move flow's. */
  const pinSrc = slice('function openPin(action, opts){', 'function closePin(dismissed){');
  check('it shows the create label only when there is no PIN yet',
    /mode===\'setup\' \? o\.setupButton : o\.loginButton/.test(pinSrc), true);
  check('and it decides that from the stored PIN', /mode=exists\?\'login\':\'setup\'/.test(pinSrc), true);
  PIN_GATE.cancel();
  fire('vpmCancel', 'click');

  console.log('\n[8] nothing is moveable when there is no positive profit');
  page.writeResult({ profit: 0, loss: -60, available: 0 }, 'GOLF');
  check('the button is disabled', els.icMoveBtn.disabled, true);
  const before = moveReqs().length;
  page.open();
  await tick();
  check('opening it moved nothing', moveReqs().length, before);
  check('and it never opened', modalOpen(), false);

  console.log('\n' + '='.repeat(60));
  if (FAILS.length) {
    console.log(FAILS.length + ' CHECK(S) FAILED:');
    FAILS.forEach(f => console.log('  - ' + f));
    process.exit(1);
  }
  console.log('ALL CHECKS PASSED');
})().catch(e => { console.error('ERROR: ' + e.message + '\n' + e.stack); process.exit(1); });
