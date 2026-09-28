/* Runs the REAL Wallet against the REAL page.
 *
 * Every <script> in index.html is stripped except the two that own the Wallet
 * (the vwd wallet block and the existing Coin Logo block), so all markup,
 * class names, ids and rendering code under test are the shipped ones. A jsdom
 * window supplies only what those two blocks read from outside: the two
 * balance ledgers and the price/coin globals.
 *
 * Needs jsdom. It is not a project dependency, so the test SKIPS (exit 0) when
 * jsdom is absent rather than failing a machine that does not have it.
 *   npm i --no-save jsdom
 *   set JSDOM_PATH=<path to node_modules\jsdom>
 *
 * Usage: node test_wallet_assets_dom.js <path to index.html>
 */
const fs = require('fs');
const path = require('path');

let JSDOM = null;
for (const p of [process.env.JSDOM_PATH, 'jsdom', path.join(__dirname, '..', 'node_modules', 'jsdom')]) {
  if (!p) continue;
  try { JSDOM = require(p).JSDOM; break; } catch (e) { /* next */ }
}
if (!JSDOM) {
  console.log('SKIP: jsdom not found. Install it, or set JSDOM_PATH.');
  process.exit(0);
}

const file = process.argv[2] || path.join(__dirname, 'index.html');
let html = fs.readFileSync(file, 'utf8');

let fails = 0;
const ok = (label, cond, detail) => {
  if (cond) console.log('  ok   ' + label);
  else { fails++; console.log('  FAIL ' + label + (detail !== undefined ? ' -- ' + JSON.stringify(detail) : '')); }
};

/* Keep only the two script blocks that own the wallet + logos, so no other
   page script can run and mask a real failure. */
async function main() {
const keep = [];
html = html.replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (m, attrs, body) => {
  const keepIt = body.includes('VWD_WALLET_COINS') || body.includes('COIN_ART=');
  if (keepIt) keep.push(body);
  return keepIt ? m : '';
});
ok('exactly one wallet script block was kept', keep.filter(b => b.includes('VWD_WALLET_COINS')).length === 1);
ok('exactly one coin-logo script block was kept', keep.filter(b => b.includes('COIN_ART=')).length === 1);

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://vanta.test/' });
const { window } = dom;
const $ = id => window.document.getElementById(id);
const wait = ms => new Promise(r => setTimeout(r, ms));

/* ---- only the globals the two wallet blocks read from outside ---------- */
window.accountMode = 'live';
window.vantaServerPrices = { USDT: 1, GOLF: 0.6, NOVA: 0.25, ABC: 0.01 };
window.VWD_DEFAULT = window.VWD_DEFAULT || { USDT: 1, GOLF: .6, NOVA: .25, ABC: .01 };
window.vantaWalletBalances = { ABC: 250, GOLF: 4, NOVA: 1.5, BTC: 0.5, ETH: 2, USDT: 900 };
window.vantaCoinBalances = { USDT: 1000, GOLF: 12, NOVA: 3, ABC: 500 };
window.VANTACOINS = [
  { sym: 'USDT', name: 'Tether' }, { sym: 'BTC', name: 'Bitcoin' }, { sym: 'ETH', name: 'Ethereum' },
  { sym: 'GOLF', name: 'Golf Coin' }, { sym: 'NOVA', name: 'Nova Coin' }, { sym: 'ABC', name: 'ABC Coin' },
];
window.showToast = m => { window.__toast = m; };
window.vantaApi = () => Promise.reject(new Error('offline in test'));
window.fetch = () => Promise.reject(new Error('no network in test'));
window.__addrCalls = [];
window.vantaShowCoinAddress = (sym, mode) => window.__addrCalls.push([sym, mode]);
/* jsdom has no canvas backend; the sheet draws a pseudo-QR, so give it a
   no-op 2D context instead of letting getContext() return null. */
window.HTMLCanvasElement.prototype.getContext = () => ({
  fillStyle: '', clearRect() {}, fillRect() {},
});

for (const body of keep) window.eval(body);
if (window.document.readyState === 'loading') {
  window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
}
await wait(50);

const COIN_ART_SRC = sym => {
  const m = html.match(new RegExp(sym + ':\\{logo:svgSrc\\(`([^`]*)`'));
  if (!m) return null;
  /* Replicate svgSrc() exactly, so the comparison is on the finished data URI
     rather than on two differently-encoded halves of it. */
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(m[1].replace(/>\s+</g, '><').trim());
};

console.log('\n[1] the Wallet lists exactly three assets');
const cards = [...window.document.querySelectorAll('#vwdAssets .vwd-asset')];
ok('three asset cards', cards.length === 3, cards.length);
const syms = cards.map(c => c.getAttribute('data-chip'));
ok('they are ABC, GOLF, NOVA', JSON.stringify(syms) === '["ABC","GOLF","NOVA"]', JSON.stringify(syms));
const listText = $('vwdAssets').textContent;
['BTC', 'ETH', 'USDT', 'LTC', 'DOGE', 'SOL', 'XRP', 'BNB', 'TRX', 'LINK', 'ADA', 'USDC']
  .forEach(s => ok('no ' + s + ' in the wallet list', listText.indexOf(s) < 0));
ok('the old 14-coin chip row is gone from the page', !$('vwdChips'));
ok('the generic operation button row is gone', !window.document.querySelector('.vwd-ops-btn'));

console.log('\n[2] the three coins use the existing Coin Logo artwork');
['ABC', 'GOLF', 'NOVA'].forEach(sym => {
  const card = window.document.querySelector('#vwdAssets [data-chip="' + sym + '"]');
  const img = card.querySelector('img.vtp-coin-logo');
  ok(sym + ' shows a real logo image, not a letter', !!img, card.innerHTML.slice(0, 120));
  if (img) {
    const expected = COIN_ART_SRC(sym);
    const actual = img.getAttribute('src');
    ok(sym + ' uses the logo from COIN_ART', actual === expected,
      { actualLen: (actual || '').length, expectedLen: (expected || '').length, sameHead: (actual || '').slice(0, 45) === (expected || '').slice(0, 45) });
  }
  ok(sym + ' names the coin', card.textContent.indexOf(sym) >= 0);
});
ok('the logo comes from the shared vantaCoinIconHTML', html.includes('vantaCoinIconHTML'));

console.log('\n[3] the hero shows the same three coins');
ok('ABC stat', !!$('vwhAbc'));
ok('GOLF stat', !!$('vwhGolf'));
ok('NOVA stat', !!$('vwhNova'));
ok('the old USDT/Portfolio stats are gone from the page', !$('vwhUsdt') && !$('vwhPortfolio'));

console.log('\n[4] balances come from the wallet ledger, not the trading one');
const abc = window.document.querySelector('#vwdAssets [data-chip="ABC"]');
ok('ABC shows the wallet balance 250', /250/.test(abc.textContent), abc.textContent);
ok('ABC does not show the trading balance 500', !/500/.test(abc.textContent));
ok('GOLF shows wallet 4', /4/.test(window.document.querySelector('#vwdAssets [data-chip="GOLF"]').textContent));
ok('a USD value is shown', /\$/.test(abc.textContent));
ok('BTC, which the wallet holds, is not listed', listText.indexOf('0.5') < 0);

console.log('\n[5] clicking a coin opens that coin\'s sheet');
ok('no sheet open initially', $('vwdModal').hidden);
window.document.querySelector('#vwdAssets [data-chip="GOLF"]').click();
ok('the sheet is open', !$('vwdModal').hidden);
ok('it is the GOLF sheet', $('vwdMSym').textContent === 'GOLF', $('vwdMSym').textContent);
ok('it shows the Golf Coin name', $('vwdMName').textContent.indexOf('Golf Coin') >= 0);
ok('the sheet head carries the GOLF logo', !!$('vwdMLogo').querySelector('img.vtp-coin-logo'));
ok('the sheet shows the wallet balance 4', /^4(\.0+)?$/.test($('vwdMBal').textContent.trim()), $('vwdMBal').textContent);
ok('the sheet shows a USD value', /2\.40/.test($('vwdMUsd').textContent), $('vwdMUsd').textContent);

console.log('\n[6] the sheet shows the wallet address and copies it');
ok('there is an address block', !!$('vwdAddr'));
ok('the address is labelled', /Wallet address/i.test($('vwdPaneDeposit').textContent));
ok('a Copy button exists', !!$('vwdCopy'));
let copied = null;
window.navigator.clipboard = { writeText: t => { copied = t; return Promise.resolve(); } };
$('vwdCopy').click();
await wait(0);
ok('Copy wrote something to the clipboard', copied !== null, copied);
ok('Copy confirms on the button', /Copied/.test($('vwdCopy').textContent), $('vwdCopy').textContent);

console.log('\n[7] all four actions are in the sheet');
['deposit', 'withdraw', 'send', 'transfer'].forEach(t => {
  const b = window.document.querySelector('#vwdTabs [data-tab="' + t + '"]');
  ok('a ' + t + ' control exists', !!b);
});
const labels = [...window.document.querySelectorAll('#vwdTabs .vwd-tab')].map(b => b.textContent.trim());
ok('they read Deposit / Withdraw / Send / Swap',
  JSON.stringify(labels) === '["Deposit","Withdraw","Send","Swap"]', JSON.stringify(labels));

console.log('\n[8] each action does the right thing');
/* Deposit shows the address pane. */
$('vwdTabs').querySelector('[data-tab="deposit"]').click();
ok('Deposit pane is visible', !$('vwdPaneDeposit').hidden);
/* Send shows the send pane. */
$('vwdTabs').querySelector('[data-tab="send"]').click();
ok('Send pane is visible', !$('vwdPaneSend').hidden && $('vwdPaneDeposit').hidden);
ok('the Send pane is the existing form, with its recipient field', !!$('vwdSendTo') && !!$('vwdSendBtn'));
/* Swap shows the convert pane, and can still target USDT. */
$('vwdTabs').querySelector('[data-tab="transfer"]').click();
ok('Swap pane is visible', !$('vwdPaneTransfer').hidden);
const targets = [...$('vwdConvTo').options].map(o => o.value);
ok('swap can still convert into USDT', targets.indexOf('USDT') >= 0, JSON.stringify(targets));
ok('swap can still convert into BTC', targets.indexOf('BTC') >= 0, JSON.stringify(targets));
/* Withdraw hands off to the existing platform address view. */
$('vwdTabs').querySelector('[data-tab="withdraw"]').click();
ok('Withdraw asked the existing address view for GOLF', JSON.stringify(window.__addrCalls.pop()) === '["GOLF","withdraw"]',
  window.__addrCalls);
ok('and it closed the sheet first', $('vwdModal').hidden);

console.log('\n[9] a coin outside the three is never offered');
window.eval("window.__vwdOpenProbe=null;");
const listed = [...window.document.querySelectorAll('#vwdAssets [data-chip]')].map(c => c.getAttribute('data-chip'));
ok('only the three are clickable', JSON.stringify(listed) === '["ABC","GOLF","NOVA"]', JSON.stringify(listed));
ok('the dashboard "My Wallets" grid is also limited to the three', (() => {
  const g = [...window.document.querySelectorAll('#vwdGrid [data-wallet]')].map(c => c.getAttribute('data-wallet'));
  return g.length === 3 && JSON.stringify(g) === '["ABC","GOLF","NOVA"]';
})(), [...window.document.querySelectorAll('#vwdGrid [data-wallet]')].map(c => c.getAttribute('data-wallet')));

console.log('\n[10] no other coin\'s address can be shown under this coin');
ok('the address view has no cross-coin USDT fallback',
  !/const val=\(map&&map\[sym\]\)\|\|\(map&&map\.USDT\)/.test(html));

console.log('\n[11] the whole thing is presentation only');
/* The strongest available check: the wallet block's own requests, balances and
   endpoints are byte-for-byte the same set as the committed version. Anything
   new would show up as a difference. */
const endpointsIn = src => {
  const m = src.match(/var VWD_ORDER[\s\S]*?\n\}\)\(\);/);
  const block = m ? m[0] : src;
  return [...new Set((block.match(/vantaApi\('[^']+'/g) || []).map(s => s.slice(9)))].sort();
};
let headHtml = '';
try {
  headHtml = require('child_process').execFileSync('git', ['show', 'HEAD:frontend/index.html'], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
} catch (e) { /* not a git checkout - skip the comparison below */ }

ok('VWD_ORDER (the swap target list) is untouched', /var VWD_ORDER = \['USDT','GOLF','NOVA','ABC','BTC'/.test(html));
ok('the display list is a separate constant', /var VWD_WALLET_COINS = \['ABC','GOLF','NOVA'\]/.test(html));
ok('the wallet still loads the one layout it always did', /vantaApi\('\/api\/wallet\/layout'\)/.test(html));
ok('no trading or swap logic was removed from the wallet block',
  /vantaApi\('\/api\/swap'/.test(html) && /vantaApi\('\/api\/wallet\/transfer'|vantaApi\('\/api\/transfer'/.test(html));
if (headHtml) {
  ok('the wallet block calls exactly the same endpoints as the committed version',
    JSON.stringify(endpointsIn(html)) === JSON.stringify(endpointsIn(headHtml)),
    { now: endpointsIn(html), head: endpointsIn(headHtml) });
  ok('the balance readers are unchanged',
    /function walletBalanceFor\(sym\)\{\s*if\(isDemo\(\)\) return Number\(practiceWallet\(\)\[sym\]\)\|\|0;/.test(html));
  ok('the price helper is unchanged', /function priceOf\(sym\)\{ var p=state\.prices\[sym\];/.test(html));
} else {
  console.log('  --   skipped the HEAD comparison (no git)');
}

console.log();
/* The wallet block installs a 20s price-refresh interval, which would keep
   Node's event loop alive forever. */
window.close();
if (fails) { console.log('FAILED (' + fails + ')'); process.exit(1); }
console.log('All real-Wallet DOM checks passed.');
process.exit(0);
}

main();
