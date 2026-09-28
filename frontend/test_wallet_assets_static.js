/* Source-level guards for the Wallet redesign. Runs anywhere (no jsdom), so
 * the invariants hold even where the DOM test skips.
 *
 * Usage: node test_wallet_assets_static.js <path to index.html>
 */
const fs = require('fs');
const path = require('path');

const file = process.argv[2] || path.join(__dirname, 'index.html');
const src = fs.readFileSync(file, 'utf8');

let fails = 0;
const ok = (label, cond, detail) => {
  if (cond) console.log('  ok   ' + label);
  else { fails++; console.log('  FAIL ' + label + (detail !== undefined ? ' -- ' + JSON.stringify(detail) : '')); }
};

console.log('\n[1] the Wallet asset list is exactly the three platform coins');
ok('a three-coin display list exists', /var VWD_WALLET_COINS = \['ABC','GOLF','NOVA'\];/.test(src));
ok('the asset list and the render loop both use it',
  /VWD_WALLET_COINS\.forEach/.test(src) && (src.match(/VWD_WALLET_COINS\.forEach/g) || []).length >= 2);
ok('nothing renders the asset list from the 14-coin order any more',
  !/VWD_ORDER\.forEach\(function\(sym\)\{\s*(?:var meta=VWD_META|html\+='<button type="button" class="vwd-card")/.test(src));
ok('VWD_ORDER itself is untouched, so swap targets still work',
  /var VWD_ORDER = \['USDT','GOLF','NOVA','ABC','BTC','ETH','BNB','SOL','XRP','LTC','TRX','DOGE','ADA','LINK','USDC'\];/.test(src));

console.log('\n[2] the old 14-chip picker and the generic op row are gone');
ok('no chip row markup', !/id="vwdChips"/.test(src));
ok('no chip row styles', !/\.vwd-chip-row\{/.test(src));
ok('no chip class is rendered', !/class="vwd-chip/.test(src));
ok('no generic op buttons in the markup', !/class="vwd-ops-btn/.test(src));
ok('the asset grid replaced them', /id="vwdAssets"/.test(src) && /\.vwd-assets\{/.test(src));
ok('each asset is a button that opens its coin', /data-asset="'\+sym\+'"[\s\S]{0,80}>/.test(src));
ok('an asset click opens that coin\'s sheet', /if\(b\.hasAttribute\('data-asset'\)\) openModal\(state\.activeSym,'deposit'\);/.test(src));

console.log('\n[3] logos come from the one existing Coin Logo system');
ok('logoHtml prefers vantaCoinIconHTML', /if\(typeof window\.vantaCoinIconHTML==='function'\)/.test(src));
ok('it only takes that path when the coin has real artwork', /if\(art&&art\.logo\)/.test(src));
ok('the old inline mark is kept as the fallback', /VWD_MARK\[sym\]/.test(src));
ok('no second, parallel icon implementation was added',
  (src.match(/function\s+logoHtml\s*\(/g) || []).length === 1);
ok('a failed logo falls back to the shared emoji handler', /vantaCoinIconFallback/.test(src));
ok('the artwork fills the existing tile', /\.vwd-logo-art>\.vtp-coin-icon\{/.test(src));
ok('the two committed inline logo slots are painted', /\[data-vfx-logo\]/.test(src));
ok('ABC, GOLF and NOVA all have artwork in COIN_ART',
  ['GOLF', 'NOVA', 'ABC'].every(s => new RegExp(s + ':\\{logo:svgSrc\\(').test(src)));

console.log('\n[4] the coin sheet has the four actions, and the address is copyable');
ok('four tabs', (src.match(/class="vwd-tab[ "]/g) || []).length === 4);
ok('labelled Deposit / Withdraw / Send / Swap',
  /data-tab="deposit"[^>]*>Deposit</.test(src) &&
  /data-tab="withdraw"[^>]*>Withdraw</.test(src) &&
  /data-tab="send"[^>]*>Send</.test(src) &&
  /data-tab="transfer"[^>]*>Swap</.test(src));
ok('the address block is labelled', /class="vwd-addr-label">Wallet address</.test(src));
ok('the Copy button is still the existing one', /id="vwdCopy"/.test(src));
ok('Copy still uses the clipboard with its legacy fallback',
  /navigator\.clipboard&&navigator\.clipboard\.writeText/.test(src) && /legacyCopy\(addr\)/.test(src));
ok('Withdraw reuses the existing platform address view',
  /vantaShowCoinAddress\(sym,'withdraw'\)/.test(src));
ok('exactly one pane is open at a time',
  /for\(var k in tabs\)\{ if\(tabs\[k\]\) tabs\[k\]\.hidden = \(k!==tab\); \}/.test(src));
ok('the address can no longer fall back to another coin\'s',
  !/\(map&&map\[sym\]\)\|\|\(map&&map\.USDT\)/.test(src));

console.log('\n[5] the hero matches the assets below it');
ok('hero shows ABC / GOLF / NOVA', /id="vwhAbc"/.test(src) && /id="vwhGolf"/.test(src) && /id="vwhNova"/.test(src));
ok('the USDT and Portfolio hero stats are gone', !/id="vwhUsdt"/.test(src) && !/id="vwhPortfolio"/.test(src));
ok('the hero total sums the same three coins',
  /const COINS=\['ABC','GOLF','NOVA'\];[\s\S]{0,200}allTotal\+=/.test(src));

console.log('\n[6] responsive');
ok('desktop/tablet/mobile breakpoints exist',
  /@media\(max-width:560px\)/.test(src) && /@media\(max-width:430px\)/.test(src));
ok('the asset grid reflows on narrow screens',
  /\.vwd-assets\{display:grid;gap:11px;grid-template-columns:repeat\(auto-fit,minmax\(210px,1fr\)/.test(src) &&
  /\.vwd-assets\{grid-template-columns:repeat\(auto-fit,minmax\(150px,1fr\)/.test(src));
ok('light theme is covered', (src.match(/\[data-theme="light"\] \.vwd-asset/g) || []).length >= 2);

console.log('\n[7] nothing else was touched');
let headSrc = '';
try {
  headSrc = require('child_process').execFileSync('git', ['show', 'HEAD:frontend/index.html'],
    { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
} catch (e) { /* not a git checkout */ }

const eps = s => [...new Set((s.match(/vantaApi\('\/api\/[a-z-]+/g) || []))].sort();
if (headSrc) {
  ok('the page calls exactly the same endpoints as the committed version',
    JSON.stringify(eps(src)) === JSON.stringify(eps(headSrc)), { now: eps(src), head: eps(headSrc) });
  ok('no new fetch target was added',
    JSON.stringify([...new Set((src.match(/api\.coingecko\.com|api\.[a-z]+\.[a-z]+/g) || []))].sort()) ===
    JSON.stringify([...new Set((headSrc.match(/api\.coingecko\.com|api\.[a-z]+\.[a-z]+/g) || []))].sort()));
  ok('no new image/asset path was introduced',
    JSON.stringify([...new Set((src.match(/(?:src|href)="(?!data:|#|https?:)[^"]+"/g) || []))].sort()) ===
    JSON.stringify([...new Set((headSrc.match(/(?:src|href)="(?!data:|#|https?:)[^"]+"/g) || []))].sort()));
} else {
  console.log('  --   skipped the HEAD comparison (no git)');
}
ok('the balance readers are untouched',
  /function walletBalanceFor\(sym\)\{\s*if\(isDemo\(\)\) return Number\(practiceWallet\(\)\[sym\]\)\|\|0;/.test(src));
ok('the trading balance reader is untouched', /function tradingBalanceFor\(sym\)\{/.test(src));
ok('the send/convert handlers are untouched', /function doSend\(\)\{/.test(src) && /function doConvert\(\)\{/.test(src));

console.log();
if (fails) { console.log('FAILED (' + fails + ')'); process.exit(1); }
console.log('All wallet static checks passed.');
