import { api, API_BASE } from './api.js';

const state = { route: 'radar', data: null, fixtureMode: false, filter: '', acknowledged: new Set(), radarTab:'new', radarRegion:'all', radarChange:'all', exploreMode:'devices', exploreSort:{devices:'latest_desc',silicon:'mobile_desc',releases:'latest_desc',sources:'latest_desc'}, productMode:'firmware', productQuery:'', productMaker:'', productRegion:'', productSort:'released_desc', maker:'all', chipVendor:'all', chipFamily:'all', chipPart:'all', android:'all', region:'all', support:'all', sourcePage:0, sourceQuery:'', sourceKind:'', sourceName:'', config:null };
state.securityFilters = {};
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const safeUrl=value=>{try{const url=new URL(String(value||''));return ['http:','https:'].includes(url.protocol)?url.href:'';}catch{return '';}};
const externalLink=(url,label,title='Open original source')=>safeUrl(url)?`<a class="source-link" href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(title)}">${escapeHtml(label)} ↗</a>`:'';

// --- gap rendering -------------------------------------------------------
// Ray asked for '-' instead of the word Unknown. The word is ALSO a data value
// the server emits and that app code filters on (x.android!=='Unknown') and a
// badge tone class (.badge.Unknown in styles.css). So the substitution happens
// HERE, at render time only; the underlying data keeps its vocabulary.
const GAP = '\u2014';
const GAP_WORDS = new Set(['', 'null', 'undefined', 'Unknown', 'Unknown target',
  'Unknown capture time', 'Date unknown', 'Source unknown', 'No prior observation']);
const isGap = v => v === null || v === undefined || GAP_WORDS.has(String(v).trim());
// A gap is a BUTTON, not a dash, because Ray wants to click the thing that is
// missing and get a brief for an agent. Context (which device, which column) is
// derived from the DOM at click time, so no template needs to pass it.
const val = (v, field) => isGap(v)
  ? `<button type="button" class="gap" data-field="${escapeHtml(field || '')}" title="Not captured - click for a research brief">${GAP}</button>`
  : escapeHtml(v);
const badge = (text, kind = text) => `<span class="badge ${escapeHtml(kind)}">${escapeHtml(text)}</span>`;
// Advisory silence label -- see docs/SOURCE_SILENCE_DETECTION.md. Never
// hides the underlying run status; only adds "this source is overdue for
// its NEXT run," which can be true even when the last run succeeded.
const silenceNote = x => x.silent
  ? `<div class="subtle">${badge('Silent · advisory','Delayed')} overdue ${Math.round(x.overdueHours)}h (expected every ~${Math.round(x.expectedIntervalHours)}h)</div>`
  : (x.silenceStatus==='insufficient_data' ? '<div class="subtle">Too few runs to judge cadence</div>' : '');
const toast = text => { const node = $('#toast'); node.textContent = text; node.classList.add('show'); setTimeout(() => node.classList.remove('show'), 2200); };

// Every key a renderer reads, with an empty value that renders as "nothing yet"
// rather than throwing. Phase 2 of the boot overwrites these; until it lands, a
// view opened early finds an empty list instead of undefined. The empties are
// never SHOWN as "no results" -- render() puts a loading panel over any view whose
// data has not arrived, because an empty table and an unloaded table look
// identical to a reader and mean opposite things.
const EMPTY_DATA = {
  updates:[], updatePage:{}, watches:[], watchlist:[],
  devices:[], devicePage:{}, chips:[], chipPage:{}, releases:[], releasePage:{},
  productReleases:[], productReleasePage:{}, productSecurity:[], productSecurityPage:{},
  sourceRecords:[], sourcePage:{}, sourceProducts:[], productPage:{},
  security:[], securityPage:{}, securityCoverage:[], health:[],
  configOptions:null, realSample:null, reviewProfiles:[], decisions:[],
  collectionRequests:[], agentBundle:null, agentProposals:[], identityHistory:[],
};

// Views that phase 1 alone can draw. Anything else waits for phase 2.
const CORE_ROUTES = ['radar', 'watchlist'];

// Rows per page, chosen by the reader and remembered. Radar used to be hardwired to
// 50 and the other tables to 100, with no way to ask for more -- on a 300-device
// catalogue that means paging to find a row you could have scanned. 500 is the
// server's cap; the control does not offer more than the server will serve.
const PAGE_SIZES = [50, 100, 250, 500];
function pageSize() {
  const stored = Number(localStorage.getItem('mo.pageSize'));
  return PAGE_SIZES.includes(stored) ? stored : 100;
}
function setPageSize(value) {
  const size = PAGE_SIZES.includes(Number(value)) ? Number(value) : 100;
  localStorage.setItem('mo.pageSize', String(size));
  return size;
}
function pageSizeControl(id) {
  return `<label class="rows-per-page">Rows
    <select id="${id}">${PAGE_SIZES.map(n =>
      `<option value="${n}"${n === pageSize() ? ' selected' : ''}>${n}</option>`).join('')}</select>
  </label>`;
}

let __loadAttempt = 0;
async function load() {
  $('#app').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  try {
    // Phase 1: only what Radar and the chrome need. The old boot awaited all 24
    // endpoints before painting anything, so first paint was hostage to the
    // slowest of them.
    const [overview, updates, acknowledgements, health, config, watches, watchlist] = await Promise.all([
      api.overview(), api.updates(radarFilters()), api.acknowledgements(),
      api.health(), api.config(), api.watches(), api.watchlist()
    ]);
    state.data = { ...EMPTY_DATA, meta: overview.meta || {}, overview,
      updates: items(updates), updatePage: updates.meta?.page || {},
      health: items(health), watches: items(watches), watchlist: items(watchlist) };
    state.config = config;
    state.acknowledged = new Set(items(acknowledgements));
    state.fixtureMode = false;
    state.pending = true;
    __loadAttempt = 0;
    state.loadError = null;
    $('#snapshotLabel').textContent = state.data.meta?.snapshot || 'Current';
    $('#navCount').textContent = state.data.overview.unseen ?? state.data.updates.length;
    render();
    loadRest();
    return;
  } catch (error) {
    // A failed load during an ingest is almost always a transient SQLite write
    // lock, not a dead server -- and the failure screen is sticky, so without
    // this the 04:15 batch leaves the app looking broken until someone clicks
    // Retry. Back off and try again a few times before giving up.
    if (__loadAttempt < 3) {
      __loadAttempt += 1;
      await new Promise(r => setTimeout(r, 1500 * __loadAttempt));
      return load();
    }
    __loadAttempt = 0;
    state.loadError = error.message || 'The API did not respond.';
    $('#snapshotLabel').textContent = 'Data unavailable';
    $('#navCount').textContent = '—';
    render();
    return;
  }
}

// Phase 2: everything the other views need, fetched after the first paint.
// Deliberately NOT here: api.agentBundle(), which was 1.1MB of a 1.6MB boot and is
// read only by the Admin tab's handoff panel -- it loads on demand in ensureAgentBundle().
async function loadRest() {
  try {
    const [devices, chips, releases, productReleases, productSecurity, sourceRecords,
           sourceProducts, security, securityCoverage, configOptions, realSample,
           reviewProfiles, decisions, collectionRequests, agentProposals, identityHistory] =
      await Promise.all([
        api.devices({limit:pageSize()}), api.chips({limit:500}), api.releases({limit:pageSize()}),
        api.productReleases({limit:pageSize()}), api.productSecurity({limit:pageSize()}),
        api.sourceRecords({limit:pageSize()}), api.sourceProducts({limit:pageSize(),state:'proposed'}),
        api.security({limit:pageSize()}), api.securityCoverage(), api.configOptions(),
        api.realSample(), api.reviewProfiles(), api.identityDecisions(),
        api.collectionRequests(), api.agentProposals(), api.identityHistory()
      ]);
    Object.assign(state.data, {
      devices:items(devices), devicePage:devices.meta?.page||{},
      chips:items(chips), chipPage:chips.meta?.page||{},
      releases:items(releases), releasePage:releases.meta?.page||{},
      productReleases:items(productReleases), productReleasePage:productReleases.meta?.page||{},
      productSecurity:items(productSecurity), productSecurityPage:productSecurity.meta?.page||{},
      sourceRecords:items(sourceRecords), sourcePage:sourceRecords.meta?.page||{},
      sourceProducts:items(sourceProducts), productPage:sourceProducts.meta?.page||{},
      security:items(security), securityPage:security.meta?.page||{},
      securityCoverage:items(securityCoverage), configOptions, realSample,
      reviewProfiles:reviewProfiles.profiles||[], decisions:items(decisions),
      collectionRequests:items(collectionRequests), agentProposals:items(agentProposals),
      identityHistory:items(identityHistory),
    });
    state.pending = false;
    state.restError = null;
  } catch (error) {
    // Phase 1 already painted, so this must not blank the app. Record it and let
    // the affected views say so instead of showing empty tables that read as
    // "no data exists".
    state.pending = false;
    state.restError = error.message || 'Could not load the full dataset.';
  }
  render();
}

// The Admin handoff panel is the only reader of the identity agent bundle. Fetch it
// the first time Admin is actually opened rather than on every boot.
let __agentBundleLoading = false;
async function ensureAgentBundle() {
  if (state.data?.agentBundle || __agentBundleLoading) return;
  __agentBundleLoading = true;
  try {
    state.data.agentBundle = await api.agentBundle();
    render();
  } catch { /* panel shows its own unavailable state */ }
  finally { __agentBundleLoading = false; }
}

function radarFilters(offset=0){return {limit:pageSize(),offset,q:state.filter,tab:state.radarTab,region:state.radarRegion==='all'?'':state.radarRegion,change:state.radarChange==='all'?'':state.radarChange};}
async function loadRadarPage(offset=0){const payload=await api.updates(radarFilters(offset));state.data.updates=items(payload);state.data.updatePage=payload.meta?.page||{};render();}
function watchButton(type,id){if(!id)return '';const enabled=(state.data.watches||[]).some(w=>w.subject_type===type&&w.subject_id===id);return `<button class="button toggle-watch" data-watch-type="${escapeHtml(type)}" data-watch-id="${escapeHtml(id)}" data-enabled="${enabled?'0':'1'}">${enabled?'★ Watching':'☆ Watch'}</button>`;}
function bindWatches(root=document){
  root.querySelectorAll('.toggle-watch').forEach(button=>{
    if(button.dataset.watchBound)return;
    button.dataset.watchBound='1';
    button.addEventListener('click',async()=>{
      button.disabled=true;
      try{
        await api.saveWatch({subjectType:button.dataset.watchType,subjectId:button.dataset.watchId,enabled:button.dataset.enabled==='1'});
        state.data.watches=items(await api.watches());
        if(state.route==='radar')await loadRadarPage(0);else render();
        button.dataset.enabled=button.dataset.enabled==='1'?'0':'1';
        button.textContent=button.dataset.enabled==='0'?'★ Watching':'☆ Watch';
        toast('Watch preference saved on this computer');
      }catch{toast('Could not save watch');}finally{button.disabled=false;}
    });
  });
}
function items(payload) { return Array.isArray(payload) ? payload : (payload?.items || []); }
async function loadSourcePage(offset=0) {
  if(state.fixtureMode)return;
  const payload=await api.sourceRecords({limit:pageSize(),offset,q:state.sourceQuery,source:state.sourceName,kind:state.sourceKind,sort:state.exploreSort.sources});
  state.data.sourceRecords=items(payload);state.data.sourcePage=payload.meta?.page||{};state.sourcePage=offset;render();
}
async function loadProductPage(offset=0) {const payload=await api.sourceProducts({limit:pageSize(),offset,state:'proposed'});state.data.sourceProducts=items(payload);state.data.productPage=payload.meta?.page||{};render();}
async function loadCanonicalPage(kind,offset=0){const method=kind==='devices'?'devices':kind==='silicon'?'chips':'releases';const filters={limit:pageSize(),offset,q:state.filter,vendor:state.chipVendor==='all'?'':state.chipVendor,family:state.chipFamily==='all'?'':state.chipFamily,part:state.chipPart==='all'?'':state.chipPart,sort:state.exploreSort[kind]};if(kind!=='silicon'){filters.maker=state.maker==='all'?'':state.maker;filters.region=state.region==='all'?'':state.region;}if(kind==='devices'){filters.max_android=state.android==='all'?'':state.android;filters.support=state.support==='all'?'':state.support;}const payload=await api[method](filters);const dataKey=kind==='devices'?'devices':kind==='silicon'?'chips':'releases';const pageKey=kind==='devices'?'devicePage':kind==='silicon'?'chipPage':'releasePage';state.data[dataKey]=items(payload);state.data[pageKey]=payload.meta?.page||{};render();}
async function loadProductEvidencePage(offset=0){const firmware=state.productMode==='firmware';const filters={limit:pageSize(),offset,q:state.productQuery,maker:state.productMaker,region:firmware?state.productRegion:'',sort:state.productSort};const payload=await (firmware?api.productReleases(filters):api.productSecurity(filters));state.data[firmware?'productReleases':'productSecurity']=items(payload);state.data[firmware?'productReleasePage':'productSecurityPage']=payload.meta?.page||{};render();}
function heading(kicker, title, body, action = '') { return `<div class="page-head"><div><div class="eyebrow">${kicker}</div><h1>${title}</h1><p>${body}</p></div>${action}${state.fixtureMode ? '<span class="demo-flag" title="API unavailable">DEMO DATA</span>' : ''}</div>`; }
function matches(row) { const q = state.filter.trim().toLowerCase(); return !q || Object.values(row).some(v => String(v).toLowerCase().includes(q)); }

function renderRadar() {
  const d = state.data, o = d.overview;
  const visible=d.updates;
  const page=d.updatePage||{};
  const cards = visible.map(x => `
    <article class="update-card">
      <div class="device-title"><div class="maker-logo">${escapeHtml(x.maker.slice(0,2).toUpperCase())}</div><div><h3><button class="entity-button" data-device="${escapeHtml(x.model)}">${escapeHtml(x.device)}</button></h3><p>${escapeHtml(x.model)} · ${escapeHtml(x.region)}</p></div></div>
      <div class="build"><span class="badge ${x.importance}">${escapeHtml(x.change)}</span><p>${escapeHtml(x.buildFrom)} → <b>${escapeHtml(x.buildTo)}</b></p></div>
      <div class="change-row"><span class="delta">Android <b>${val(x.androidFrom,'Android version (previous)')} → ${val(x.androidTo,'Android version')}</b></span><span class="delta">Patch <b>${val(x.patchTo,'security patch level')}</b></span>${watchButton(x.subjectType,x.subjectId)}</div>
      <div class="time">Detected ${escapeHtml(x.detectedAt||x.age)}<br><span class="subtle">Effective ${val(x.effectiveAt,'effective date')}</span><br><button class="button acknowledge" data-id="${escapeHtml(x.id)}">Mark seen</button></div>
    </article>`).join('');
  return heading('Update Radar','What changed since your last visit','A precise feed of new firmware for the devices and regions you care about.') + `
    <div class="metrics"><div class="metric"><small>New firmware</small><strong>${o.unseen}</strong><span>since your last visit</span></div><div class="metric"><small>Android upgrades</small><strong>${o.androidUpgrades}</strong><span>major version changes</span></div><div class="metric"><small>Security patches</small><strong>${o.securityPatches}</strong><span>new patch levels</span></div><div class="metric"><small>Source warnings</small><strong>${o.sourceWarnings}</strong><span>last run ${o.lastRun}</span></div></div>
    <div class="toolbar"><div class="segmented" id="radarTabs"><button data-value="new" class="${state.radarTab==='new'?'active':''}">New</button><button data-value="watched" class="${state.radarTab==='watched'?'active':''}">Watched</button><button data-value="history" class="${state.radarTab==='history'?'active':''}">History</button></div><select id="radarRegion"><option value="all">All regions</option><option value="ILO">Israel / ILO</option><option value="MID">Middle East / MID</option><option value="GLOBAL">Global</option></select><select id="radarChange"><option value="all">All changes</option><option value="Android upgrade">Android upgrades</option><option value="Security patch">Security patches</option></select><span class="spacer"></span>${state.radarTab==='new'?'<button class="button" id="markAll">Mark visible seen</button>':''}</div>
    <div class="validation-note">A dash (—) means the captured vendor artifact did not state that value — it is not an empty value and not a guess. Build names are never guessed into Android versions. Click any dash for a research brief, or a device name for its known history.</div><div class="subtle">${page.total?page.offset+1:0}–${(page.offset||0)+visible.length} of ${page.total||0} matching events</div><div class="feed">${cards || `<div class="empty"><b>No ${state.radarTab} updates match.</b><br>Try another region or change type. This does not mean source coverage is complete.</div>`}</div><div class="toolbar">${pageSizeControl('radarRows')}<button class="button" id="radarPrev" ${page.offset?'':'disabled'}>← Previous</button><button class="button" id="radarNext" ${page.nextCursor?'':'disabled'}>Next →</button></div>`;
}

const deviceRows = rows => rows.map(x => `<tr><td><button class="entity-button" data-device="${escapeHtml(x.model)}">${escapeHtml(x.name)}</button><div class="subtle">${escapeHtml(x.maker)} · ${escapeHtml(x.model)}</div>${watchButton('hardware_model',x.id)}</td><td>${isGap(x.chip)&&isGap(x.part)?val(x.chip,'chipset'):`<button class="entity-button" data-chip="${escapeHtml(x.part)}">${escapeHtml(x.chip)}</button>`+(isGap(x.part)?'':`<div class="subtle">${escapeHtml(x.part)}</div>`)}</td><td><b>Android ${val(x.android,'Android version')}</b><div class="subtle">SPL ${val(x.patch,'security patch level')}</div><small>${escapeHtml(({source_manifest_latest:"Latest in captured regional manifest",vendor_release_date:"Latest known vendor release date",observation_order_only:"Latest firmware not established"})[x.software_state_basis]||"Software evidence unavailable")}</small></td><td>${escapeHtml(x.region)}</td><td>${isGap(x.support)?val(x.support,'support status'):badge(x.support,x.support==='Supported'?'good':'Unknown')}</td><td>${badge(x.confidence,'good')}</td></tr>`).join('');
const chipRows = rows => rows.map(x => `<tr><td><button class="entity-button" data-chip="${escapeHtml(x.part)}">${escapeHtml(x.name)}</button><div class="subtle">${escapeHtml(x.vendor)} · ${escapeHtml(x.family)}</div></td><td><button class="entity-button" data-chip="${escapeHtml(x.part)}">${escapeHtml(x.part)}</button></td><td><b>${x.devices} mobile links</b><div class="subtle">Reviewed hardware: ${x.canonical_devices} · Product evidence: ${x.product_devices}</div></td><td>${x.advisories}</td><td>${x.open ? badge(`${x.open} without CVE fix coordinate`,'Unknown') : badge(x.advisories?'CVE coordinates captured':'No linked CVEs','Unknown')}</td></tr>`).join('');
const releaseRows = rows => rows.map(x=>`<tr><td><button class="entity-button" data-device="${escapeHtml(x.model)}">${escapeHtml(x.device)}</button><div class="subtle">${escapeHtml(x.maker)} · ${escapeHtml(x.model)}</div></td><td>${val(x.region,'region')}</td><td><span class="strong">${escapeHtml(x.build)}</span><div class="subtle">${escapeHtml(x.channel)} ${externalLink(x.source_url,'source')}</div></td><td>Android ${val(x.android,'Android version')}</td><td>${val(x.patch,'security patch level')}</td><td>${val(x.baseband,'baseband version')}</td><td>${val(x.released,'release date')}</td></tr>`).join('');
// The identity cell links when the row resolves to a canonical device and stays
// plain text when it does not. Previously the whole column was plain text, so a
// model code that opens from Explore > Devices was dead here -- the same value
// clickable in one tab and inert in another. `canonical_model` is the server's
// answer to "can this be opened", so a link never 404s and an unresolved row is
// never dressed up as one that resolves.
const sourceIdentity = x => {
  const label = x.source_name||x.device||x.model_code||x.source_key;
  if (x.canonical_model)
    return `<button class="entity-button" data-device="${escapeHtml(x.canonical_model)}">${escapeHtml(label)}</button>`;
  // A product link is a weaker claim than a hardware one, so it opens the product
  // record -- which says so itself -- rather than dressing this up as a device.
  if (x.canonical_product)
    return `<button class="entity-button" data-product="${escapeHtml(x.canonical_product)}">${escapeHtml(label)}</button>`;
  return `<b>${escapeHtml(label)}</b>`;
};
const sourceRows = rows => rows.map(x=>`<tr><td>${sourceIdentity(x)}<div class="subtle">${escapeHtml(x.source_key)}</div></td><td>${escapeHtml(x.source)}<div class="subtle">${externalLink(x.download_url,'download','Download ROM')||externalLink(x.source_url,'source')}</div></td><td>${badge(x.kind,'Unknown')}</td><td>${escapeHtml(x.build||x.patch||'—')}<div class="subtle">${x.android?`Android ${escapeHtml(x.android)}`:''}</div></td><td>${val(x.region,'region')}</td><td>${badge(x.identity_state||x.validation_state,x.identity_state?'Unknown':'good')}</td><td><b>${escapeHtml(x.effective_at)}</b><div class="subtle">observed ${escapeHtml(x.observed_at)}</div></td></tr>`).join('');
const productRows = rows => rows.map(x=>`<tr><td><b>${escapeHtml(x.name)}</b><div class="subtle">${escapeHtml(x.maker)}</div></td><td>${x.identities}</td><td>${x.observations}</td><td>${escapeHtml(x.chipset||'Awaiting specification match')}<div class="subtle">${escapeHtml(x.launch_os||'')}</div></td><td>${badge(x.review_state,x.review_state==='approved'?'good':'Unknown')}</td><td><button class="button product-review" data-id="${escapeHtml(x.id)}" data-decision="approved">Approve</button> <button class="button product-review" data-id="${escapeHtml(x.id)}" data-decision="rejected">Reject</button></td></tr>`).join('');

function renderExplore() {
  const chipByPart=new Map(state.data.chips.map(x=>[x.part,x]));
  const contains=(value,query)=>!query||query==='all'||String(value||'').toLowerCase().includes(String(query).toLowerCase());
  const chipMatch=x=>{const c=chipByPart.get(x.part)||{};return contains(c.vendor,state.chipVendor)&&contains(c.family,state.chipFamily)&&contains(x.part,state.chipPart)};
  const devices=state.data.devices.filter(matches).filter(x=>state.maker==='all'||x.maker===state.maker).filter(chipMatch).filter(x=>state.android==='all'||(x.android!=='Unknown'&&x.android<=Number(state.android))).filter(x=>state.region==='all'||x.region.toLowerCase().includes(state.region)).filter(x=>state.support==='all'||x.support===state.support);
  const chips=state.data.chips.filter(matches).filter(x=>contains(x.vendor,state.chipVendor)).filter(x=>contains(x.family,state.chipFamily)).filter(x=>contains(x.part,state.chipPart));
  const releases=state.data.releases.filter(matches).filter(x=>state.maker==='all'||x.maker===state.maker).filter(x=>state.region==='all'||String(x.region).toLowerCase().includes(state.region));
  const sourceRecords=state.data.sourceRecords||[], sourcePage=state.data.sourcePage||{};
  const families=[...new Set(state.data.chips.filter(x=>state.chipVendor==='all'||x.vendor.toLowerCase()===state.chipVendor).map(x=>x.family))].sort();
  const parts=[...new Set(state.data.chips.filter(x=>(state.chipVendor==='all'||x.vendor.toLowerCase()===state.chipVendor)&&(state.chipFamily==='all'||x.family===state.chipFamily)).map(x=>x.part))].sort();
  return heading('Device & Silicon Explorer','Find the exact hardware','Filter devices by identity, Android version, region, and exact silicon part.') + `
  <div class="filters"><label>Manufacturer<select id="makerFilter"><option value="all">All manufacturers</option>${[...new Set(state.data.devices.map(x=>x.maker))].sort().map(x=>`<option>${escapeHtml(x)}</option>`).join('')}</select></label><label>Chip vendor contains<input class="filter-input" id="chipFilter" list="vendorOptions" value="${escapeHtml(state.chipVendor==='all'?'':state.chipVendor)}" placeholder="Type Qualcomm, MediaTek…"><datalist id="vendorOptions">${[...new Set(state.data.chips.map(x=>x.vendor))].sort().map(x=>`<option value="${escapeHtml(x)}">`).join('')}</datalist></label><label>Chip family contains<input class="filter-input" id="familyFilter" list="familyOptions" value="${escapeHtml(state.chipFamily==='all'?'':state.chipFamily)}" placeholder="Type Snapdragon, Dimensity…"><datalist id="familyOptions">${families.map(x=>`<option value="${escapeHtml(x)}">`).join('')}</datalist></label><label>Part contains<input class="filter-input" id="partFilter" list="partOptions" value="${escapeHtml(state.chipPart==='all'?'':state.chipPart)}" placeholder="Type SM8750, MT…"><datalist id="partOptions">${parts.map(x=>`<option value="${escapeHtml(x)}">`).join('')}</datalist></label><label>Android ceiling<select id="androidFilter"><option value="all">Any version</option><option value="17">17 or lower</option><option value="16">16 or lower</option><option value="15">15 or lower</option></select></label><label>Region<select id="regionFilter"><option value="all">Any region</option><option value="ilo">Israel / ILO</option><option value="mid">Middle East / MID</option><option value="global">Global</option></select></label><label>Support<select id="supportFilter"><option value="all">All statuses</option><option value="Supported">Officially supported</option><option value="Likely supported">Likely supported</option><option value="End announced">End announced</option><option value="Unsupported">Unsupported</option><option value="Unknown">Not stated by vendor</option></select></label></div>
  <div class="toolbar"><div class="segmented" id="exploreTabs"><button data-value="devices" class="${state.exploreMode==='devices'?'active':''}">Devices (${state.data.devicePage?.total||devices.length})</button><button data-value="silicon" class="${state.exploreMode==='silicon'?'active':''}">Silicon (${state.data.chipPage?.total||chips.length})</button><button data-value="releases" class="${state.exploreMode==='releases'?'active':''}">Canonical ROMs (${state.data.releasePage?.total||releases.length})</button><button data-value="sources" class="${state.exploreMode==='sources'?'active':''}">Source records (${sourcePage.total||0})</button></div><span class="subtle">${state.exploreMode==='sources'?`${sourcePage.offset+1}-${sourcePage.offset+sourceRecords.length} of ${sourcePage.total||0}`:`${((state.data[state.exploreMode==='devices'?'devicePage':state.exploreMode==='silicon'?'chipPage':'releasePage']?.offset)||0)+1}-${((state.data[state.exploreMode==='devices'?'devicePage':state.exploreMode==='silicon'?'chipPage':'releasePage']?.offset)||0)+(state.exploreMode==='devices'?devices.length:state.exploreMode==='silicon'?chips.length:releases.length)} shown`}</span><span class="spacer"></span>${state.exploreMode!=='sources'?`<button class="button" id="canonicalPrev">←</button><button class="button" id="canonicalNext">→</button>`:''}${pageSizeControl('exploreRows')}<button class="button" id="exportView">Export CSV ↓</button></div>
  ${state.exploreMode==='devices'?`<h2 class="section-title">Reviewed devices</h2><div class="data-card">${devices.length?`<table class="data-table"><thead><tr><th>Device identity</th><th>Silicon</th><th>Software state</th><th>Availability</th><th>Support</th><th>Evidence</th></tr></thead><tbody>${deviceRows(devices)}</tbody></table>`:'<div class="empty"><b>No matching devices.</b><br>No result may also mean incomplete source coverage; check Admin health.</div>'}</div>`:state.exploreMode==='silicon'?`<h2 class="section-title">Silicon index</h2><div class="data-card">${chips.length?`<table class="data-table"><thead><tr><th>Chip</th><th>Exact part</th><th>Used by</th><th>Advisories</th><th>Attention</th></tr></thead><tbody>${chipRows(chips)}</tbody></table>`:'<div class="empty"><b>No matching silicon.</b><br>Try a broader chip filter.</div>'}</div>`:state.exploreMode==='releases'?`<h2 class="section-title">Canonical ROM versions</h2><div class="data-card">${releases.length?`<table class="data-table"><thead><tr><th>Model</th><th>Region</th><th>Build</th><th>OS</th><th>Security patch</th><th>Baseband</th><th>Released</th></tr></thead><tbody>${releaseRows(releases)}</tbody></table>`:'<div class="empty"><b>No observed ROMs match.</b><br>This is different from proof that no ROM exists.</div>'}</div>`:`<h2 class="section-title">All captured source records</h2><div class="notice">These rows are visible evidence awaiting or supporting canonical identity resolution. “Unresolved” is intentional—not missing data.</div><div class="toolbar"><input id="sourceQuery" value="${escapeHtml(state.sourceQuery)}" placeholder="Search name, codename, build…"><select id="sourceName"><option value="">All sources</option><option value="xiaomi.community.firmware_tracker">Xiaomi (${state.data.health.find(x=>x.source.includes('xiaomi'))?.records||0})</option><option value="tecno.vendor.security_device_scope">Tecno (${state.data.health.find(x=>x.source.includes('tecno'))?.records||0})</option><option value="samsung.fota">Samsung (${state.data.health.find(x=>x.source.includes('samsung'))?.records||0})</option></select><select id="sourceKind"><option value="">All record types</option><option value="firmware_release">Firmware</option><option value="security_patch_publication">Security patch publication</option></select><button class="button primary" id="sourceSearch">Search</button></div><div class="data-card"><table class="data-table"><thead><tr><th>Source identity</th><th>Collector</th><th>Record type</th><th>Observed value</th><th>Region</th><th>Identity state</th><th>Observed</th></tr></thead><tbody>${sourceRows(sourceRecords)}</tbody></table></div><div class="toolbar"><button class="button" id="sourcePrev" ${sourcePage.offset?'':'disabled'}>← Previous</button><span class="subtle">Rows ${sourcePage.total?sourcePage.offset+1:0}-${sourcePage.offset+sourceRecords.length} of ${sourcePage.total||0}</span><button class="button" id="sourceNext" ${sourcePage.nextCursor?'':'disabled'}>Next →</button></div>`}`;
}

let securityPageRequest=0;
async function loadSecurityPage(offset=0){const request=++securityPageRequest;const payload=await api.security({...state.securityFilters,limit:pageSize(),offset});if(request!==securityPageRequest)return;state.data.security=items(payload);state.data.securityPage=payload.meta?.page||{};render();}
function renderSecurity() {
  const rows=state.data.security,page=state.data.securityPage||{},f=state.securityFilters,total=page.total??rows.length;
  return heading('Security Relations','Evidence, connected','Trace a CVE through captured claims, exact silicon, hardware mappings, and adjudicated verdicts.') + `
  <div class="notice"><b>${total} matching captured CVEs.</b>${f.silicon_vendor?' Exact silicon vendor: '+escapeHtml(f.silicon_vendor)+'.':''} A fix coordinate is a source claim, not proof that a device is fixed. Hardware links identify reviewed devices containing a claimed affected part; constraints and firmware must still be evaluated. Missing evidence never means safe.</div>
  <form id="securityFilters"><div class="toolbar"><input id="securityQuery" aria-label="Search CVEs" value="${escapeHtml(f.q||'')}" placeholder="CVE, component, bulletin…"><input id="securityVendor" aria-label="Bulletin vendor or source" value="${escapeHtml(f.vendor||'')}" placeholder="Bulletin vendor/source…"><button class="button primary" type="submit">Apply</button><button class="button" type="button" id="securityReset">Reset</button></div>
  <details ${Object.keys(f).some(k=>!['q','vendor'].includes(k)&&f[k])?'open':''}><summary>Advanced evidence filters</summary><p class="subtle">Monthly bulletins match any overlapping date range; the exact publication day is not captured.</p><div class="form-grid"><label>Publication range from<input type="date" id="securityFrom" value="${escapeHtml(f.date_from||'')}"></label><label>Publication range through<input type="date" id="securityTo" value="${escapeHtml(f.date_to||'')}"></label><label>Exact part<input id="securityPart" value="${escapeHtml(f.part||'')}" placeholder="e.g. MT6789"></label><label>Exact reviewed model<input id="securityModel" value="${escapeHtml(f.model||'')}" placeholder="e.g. SM-A155F"></label><label>CVE fix coordinate<select id="securityFix"><option value="">Any captured status</option><option value="with_coordinate" ${f.fix_status==='with_coordinate'?'selected':''}>Coordinate captured</option><option value="without_coordinate" ${f.fix_status==='without_coordinate'?'selected':''}>No coordinate captured</option></select></label><label>Publication order<select id="securitySort"><option value="">Newest first</option><option value="oldest_asc" ${f.sort==='oldest_asc'?'selected':''}>Oldest first</option></select></label></div><div class="toolbar"><label><input type="checkbox" id="securityMobile" ${f.mobile_linked?'checked':''}> Reviewed hardware links only</label><label><input type="checkbox" id="securityExact" ${f.exact_part?'checked':''}> Exact affected part only</label></div></details></form>
  <details><summary>Source coverage gaps</summary><div class="data-card flow">${(state.data.securityCoverage||[]).map(x=>`<div class="flow-step"><i>${x.status==='available'?'✓':'!'}</i><div><b>${escapeHtml(x.vendor)} · ${escapeHtml(x.capability)} · ${escapeHtml(x.status)}</b><div class="subtle">${escapeHtml(x.reason)}</div></div></div>`).join('')}</div></details>
  <div class="toolbar"><button class="button" id="securityPrev" ${page.offset?'':'disabled'}>← Previous</button><span class="subtle">Rows ${total?(page.offset||0)+1:0}–${(page.offset||0)+rows.length} of ${total}</span><button class="button" id="securityNext" ${page.nextCursor?'':'disabled'}>Next →</button></div>
  <div class="data-card">${rows.length?`<table class="data-table"><thead><tr><th>Finding / date</th><th>Component → silicon</th><th>Hardware links</th><th>Captured evidence</th></tr></thead><tbody>${rows.map(x=>`<tr><td><button class="entity-button security-detail" data-security-cve="${escapeHtml(x.cve)}">${escapeHtml(x.cve)}</button><div class="subtle">${val(x.published_at,'published date')}${x.published_precision==='month'?' (bulletin month)':''} · ${escapeHtml(x.bulletin)}</div></td><td>${escapeHtml(x.component)}<div class="subtle">${escapeHtml(String(x.chip).split(',').slice(0,2).join(', '))}${String(x.chip).split(',').length>2?' · +'+(String(x.chip).split(',').length-2)+' more':''}</div></td><td>${x.devices}</td><td><button class="entity-button security-detail" data-security-cve="${escapeHtml(x.cve)}">${escapeHtml(x.state)} · View chain →</button><div class="subtle">${escapeHtml(x.reasoning)}</div></td></tr>`).join('')}</tbody></table>`:'<div class="empty">No captured CVEs match these filters. Coverage may be incomplete.</div>'}</div>`;
}

function renderProducts(){const firmware=state.productMode==='firmware',rows=(firmware?state.data.productReleases:state.data.productSecurity)||[],page=state.data[firmware?'productReleasePage':'productSecurityPage']||{},known=v=>v&&v!=='null'&&v!=='Unknown'?v:GAP,spec=x=>x.spec_slug?`https://www.gsmarena.com/${x.spec_slug}`:'';return heading('Resolved product evidence','Useful facts before exact hardware identity is proven','Captured firmware and security statements remain useful when a product name is known and its exact hardware model is unresolved.')+`<div class="notice"><b>What this is:</b> a safe middle layer between raw source records and canonical hardware. It prevents the system from discarding useful firmware/security facts or inventing a model-code match. A dash (—) means more evidence is needed — click one for a research brief.</div><div class="toolbar"><div class="segmented" id="productEvidenceTabs"><button data-value="firmware" class="${firmware?'active':''}">Firmware (${state.data.productReleasePage?.total||0})</button><button data-value="patches" class="${!firmware?'active':''}">Security publications (${state.data.productSecurityPage?.total||0})</button></div><input id="productQuery" value="${escapeHtml(state.productQuery)}" placeholder="Product, codename, build…"><select id="productMaker"><option value="">All makers</option><option value="Xiaomi">Xiaomi</option><option value="TECNO">TECNO</option><option value="HMD">HMD</option><option value="Nokia">Nokia</option></select>${firmware?'<input id="productRegion" value="'+escapeHtml(state.productRegion)+'" placeholder="Exact region…">':''}<select id="productSort"><option value="released_desc">Newest first</option><option value="released_asc">Oldest first</option><option value="product_asc">Product A–Z</option>${firmware?'<option value="android_desc">Android highest first</option>':''}</select><button class="button primary" id="productSearch">Apply</button><button class="button" id="productReset">Reset</button></div><div class="subtle product-count">Rows ${page.total?page.offset+1:0}-${(page.offset||0)+rows.length} of ${page.total||0}</div><div class="data-card"><table class="data-table"><thead><tr>${firmware?'<th>Product</th><th>Source identity</th><th>Region</th><th>Build / links</th><th>Android</th><th>Released</th><th>Evidence gap</th>':'<th>Product</th><th>Source identity</th><th>Patch statement</th><th>Published</th><th>Title / links</th><th>Evidence gap</th>'}</tr></thead><tbody>${rows.map(x=>firmware?`<tr><td><button class="entity-button" data-product="${escapeHtml(x.product_id)}">${escapeHtml(x.device)}</button><div class="subtle">${escapeHtml(x.maker)} · ${externalLink(spec(x),'GSMArena','Open captured specification source')}</div></td><td>${escapeHtml(x.source_identity)}</td><td>${escapeHtml(x.region)}</td><td><b>${escapeHtml(x.build)}</b><div class="subtle">${escapeHtml(x.channel)} · ${externalLink(x.download_url,'download ROM','Open ROM download')||externalLink(x.source_url,'source')}</div></td><td>${escapeHtml(known(x.android))}</td><td>${escapeHtml(known(x.released))}</td><td>${x.source==='xiaomi.community.firmware_tracker'?`<button class="button research-product" data-target="${escapeHtml(x.source_identity)}" data-source="xiaomi" data-scope="device_profile">${known(x.android)===GAP?'Research missing profile':'Research exact identity'}</button>`:`<button class="button" data-product="${escapeHtml(x.product_id)}">Inspect captured evidence</button>`}</td></tr>`:`<tr><td><button class="entity-button" data-product="${escapeHtml(x.product_id)}">${escapeHtml(x.device)}</button><div class="subtle">${escapeHtml(x.maker)} · ${externalLink(spec(x),'GSMArena')}</div></td><td>${escapeHtml(x.source_identity)}</td><td><b>${escapeHtml(x.security_patch_level||x.patch)}</b></td><td>${escapeHtml(known(x.published_at))}${x.first_live_release?`<small>First release: ${escapeHtml(x.first_live_release)}</small>`:''}</td><td>${escapeHtml(x.title||'')}<div class="subtle">${externalLink(x.source_url,'original source')}</div></td><td>${x.source==='tecno.vendor.security_device_scope'?`<button class="button research-product" data-target="${escapeHtml(x.source_identity)}" data-source="tecno" data-scope="security">Refresh evidence</button>`:`<button class="button" data-product="${escapeHtml(x.product_id)}">Inspect captured evidence</button>`}</td></tr>`).join('')}</tbody></table></div><div class="toolbar"><button class="button" id="productEvidencePrev" ${page.offset?'':'disabled'}>← Previous</button><button class="button" id="productEvidenceNext" ${page.nextCursor?'':'disabled'}>Next →</button></div>`;}

function openChip(part){const c=state.data.chips.find(x=>x.part===part);if(!c)return;const devices=state.data.devices.filter(x=>x.part===part),products=c.products||[],findings=c.security?.items||[];openDetail(`<div class="detail-head"><div><div class="eyebrow">Mobile silicon</div><h2>${escapeHtml(c.name)}</h2><div class="subtle">${escapeHtml(c.vendor)} · ${escapeHtml(c.family)} · ${escapeHtml(c.part)}</div></div><button class="button detail-close">Close</button></div><div class="detail-body"><div class="notice"><b>${c.canonical_devices} reviewed hardware mappings</b> and <b>${c.product_devices} product-level specification mappings</b>. These confidence levels stay separate.</div><section class="detail-section"><h3>Reviewed hardware models</h3>${devices.length?`<div class="data-card"><table class="data-table"><tbody>${devices.map(x=>`<tr><td><button class="entity-button" data-device="${escapeHtml(x.model)}">${escapeHtml(x.name)}</button><div class="subtle">${escapeHtml(x.model)}</div></td><td>Android ${escapeHtml(x.android)}</td><td>${escapeHtml(x.region)}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">No exact hardware-model mapping captured.</div>'}</section><section class="detail-section"><h3>Mobile products from specification evidence</h3>${products.length?products.map(x=>`<div class="detail-fact"><button class="entity-button" data-product="${escapeHtml(x.id)}">${escapeHtml(x.name)}</button><small>Product specification · ${escapeHtml(x.confidence)} confidence</small>${(x.evidence||[]).filter(e=>e.source_url).map(e=>externalLink(e.source_url,'Captured specification')).join(' ')}</div>`).join(''):'<div class="empty">No product-level mapping captured.</div>'}</section><section class="detail-section"><h3>Security advisories and findings · ${c.security?.meta?.page?.total||0}</h3>${c.security?.meta?.page?.nextCursor?`<button class="button" id="chipAllFindings">View all linked CVEs in Security</button><p class="subtle">Showing first ${findings.length} captured CVEs.</p>`:''}${findings.length?findings.map(x=>`<button class="detail-fact entity-button security-inline" data-security-cve="${escapeHtml(x.cve)}"><b>${escapeHtml(x.cve)}</b> · ${escapeHtml(x.state)}<small>${escapeHtml(x.bulletin)} · ${escapeHtml(x.reasoning)}</small></button>`).join(''):'<div class="notice">No linked findings in this snapshot. Coverage may be incomplete.</div>'}</section></div>`);panelSecurityLinks();}
let detailRequest = 0;
const matchLabel = method => ({exact_product_name:'Exact product name',exact_catalog_codename:'Exact catalog codename',exact_play_identifier:'Exact Google Play identifier',standalone_captured_specification:'Captured specification only'}[method] || method);
const knownValue = value => value && value !== 'null' ? value : 'Unknown';
function siliconEvidence(silicon) {
  if (!silicon) return '<div class="notice">No chipset specification captured for this product. Coverage is incomplete.</div>';
  return `<div class="detail-fact"><b>${escapeHtml(silicon.raw_chipset)}</b><small>Product specification · ${escapeHtml(silicon.confidence)} confidence · observed ${escapeHtml(silicon.observed_at)}</small></div>${(silicon.evidence||[]).map(e => `<div class="detail-fact">${externalLink(e.source_url, e.device_name || e.source)}<small>${escapeHtml(matchLabel(e.match_method) || e.source)}${e.locator?' · '+escapeHtml(e.locator):''}</small>${e.artifact_sha256?`<details><summary>Captured evidence</summary><p class="evidence-hash">SHA-256: ${escapeHtml(e.artifact_sha256)}</p>${e.codename?`<p>Captured codename: ${escapeHtml(e.codename)}</p>`:''}${e.models?.length?`<p>Models listed by this community source: ${escapeHtml(e.models.join(', '))}</p><small>These source identifiers do not create canonical hardware mappings.</small>`:''}${e.source_commit?`<p class="evidence-hash">Source commit: ${escapeHtml(e.source_commit)}</p>`:''}${e.license?`<p>${escapeHtml(e.attribution||e.source)} · ${escapeHtml(e.license)}</p>`:''}${e.specification?`<dl>${['cpu','gpu','os','wlan','bluetooth','internalmemory'].filter(k=>e.specification[k]).map(k=>`<dt>${escapeHtml(k==='os'?'Launch OS (specification)':k)}</dt><dd>${escapeHtml(e.specification[k])}</dd>`).join('')}</dl>`:''}</details>`:''}</div>`).join('')}`;
}
function sourceBuildsContent(page) {
  if(!page?.items?.length)return '<p>No source-build catalog evidence captured.</p>';
  return `<p class="subtle">Source-code build targets, not OTA releases or current support. Patch dates are source statements, not device safety assessments.</p><div class="data-card"><table class="data-table"><thead><tr><th>Source build</th><th>Android source release</th><th>Patch statement</th></tr></thead><tbody>${page.items.map(x=>`<tr><td><b>${escapeHtml(x.build_id)}</b><small>${escapeHtml(x.source_tag)}</small><div>${externalLink(x.source_url,'Official source')}</div><details><summary>Evidence</summary><small>${escapeHtml(x.locator)} · observed ${escapeHtml(x.observed_at)}</small><p class="evidence-hash">${escapeHtml(x.artifact_sha256)}</p></details></td><td>${val(x.android_version||x.version_label,'Android version')}</td><td>${val(x.security_patch_level,'security patch level')}</td></tr>`).join('')}</tbody></table></div><p class="subtle">${page.items.length} of ${page.meta.page.total} source-build statements</p>${page.meta.page.nextCursor?'<button class="button" id="sourceBuildsMore">Load next 50 source builds</button>':''}`;
}
function productHistoryRows(rows, firmware) {
  return rows.map(x => firmware ? `<tr><td>${escapeHtml(x.region)}<div class="subtle">${escapeHtml(x.channel)}</div></td><td><b>${escapeHtml(x.build)}</b><div class="subtle">${externalLink(x.download_url,'Download ROM')} ${externalLink(x.source_url,'Original source')}</div><small>${escapeHtml(x.delivery_method||'')}</small>${x.security_patch_level?`<small>Vendor patch statement: ${escapeHtml(x.security_patch_level)}</small>`:''}${x.vendor_comments?`<small>${escapeHtml(x.vendor_comments)}</small>`:''}${x.release_scope?`<small>${escapeHtml(x.release_scope)}</small>`:''}</td><td>${escapeHtml(knownValue(x.android))}</td><td>${escapeHtml(knownValue(x.released))}<div class="subtle">Observed ${escapeHtml(knownValue(x.observed))}</div></td></tr>` : `<tr><td>${escapeHtml(x.security_patch_level||x.patch)}</td><td>${escapeHtml(knownValue(x.published_at))}${x.first_live_release?`<div class="subtle">First live release: ${escapeHtml(x.first_live_release)}</div><small>${escapeHtml(x.release_scope||'')}</small>`:''}</td><td>${escapeHtml(x.title||'')}<div class="subtle">${externalLink(x.source_url,'Original publication')}</div></td></tr>`).join('');
}
async function openProductRemote(id) {
  const request = ++detailRequest;
  openDetail('<div class="detail-head"><h2>Product evidence</h2><button class="button detail-close">Close</button></div><div class="detail-body" aria-live="polite">Loading product history…</div>');
  try {
    const payload = await api.productDetail(id);
    if (request !== detailRequest) return;
    const p=payload.product, conclusion=payload.identityConclusion;
    const history = (page,firmware) => `<section class="detail-section"><h3>${firmware?'ROM history':'Security publications'} · ${page.meta.page.total}</h3>${firmware&&payload.regions.length?`<div class="toolbar"><select id="detailRegion" aria-label="Product ROM region"><option value="">All regions</option>${[...new Set(payload.regions.map(r=>r.region))].map(r=>`<option>${escapeHtml(r)}</option>`).join('')}</select><select id="detailChannel" aria-label="Product ROM channel"><option value="">All channels</option>${[...new Set(payload.regions.map(r=>r.channel))].map(r=>`<option>${escapeHtml(r)}</option>`).join('')}</select></div>`:''}<div id="${firmware?'productFirmware':'productPublications'}">${historyContent(page,firmware)}</div></section>`;
    openDetail(`<div class="detail-head"><div><div class="eyebrow">Product evidence · ${escapeHtml(p.manufacturer)}</div><h2>${escapeHtml(p.canonical_name)}</h2>${watchButton('source_product',p.id)}<div class="subtle">Last observed ${escapeHtml(payload.lastObserved)}</div></div><button class="button detail-close">Close</button></div><div class="detail-body"><div class="notice"><b>Product identity: ${escapeHtml(p.review_state)}</b> · ${escapeHtml(conclusion?.confidence||payload.silicon?.confidence||GAP)} confidence.<br>Exact hardware identity and security applicability are not established by this product record.</div>${conclusion?`<p>${escapeHtml(conclusion.rationale)}</p>`:''}<section class="detail-section"><details><summary>Source names and codenames · ${payload.identities.length}</summary>${payload.identities.map(i=>`<div class="detail-fact"><b>${escapeHtml(i.source_value)}</b><small>${escapeHtml(i.namespace)} · ${escapeHtml(i.source_name)} · ${escapeHtml(i.confidence)} · ${escapeHtml(i.resolution_state)}</small></div>`).join('')||'<p>No source aliases captured.</p>'}${conclusion?.evidence?.some(e=>e.catalog_names)?`<details><summary>Captured catalog names</summary><p>${escapeHtml([...new Set(conclusion.evidence.flatMap(e=>e.catalog_names||[]))].join(' · '))}</p></details>`:''}${conclusion?.candidates?.length?`<details><summary>Identity candidates (not established aliases)</summary><p>${escapeHtml(conclusion.candidates.join(' · '))}</p></details>`:''}</details></section><section class="detail-section"><h3>Chipset and specification evidence</h3>${siliconEvidence(payload.silicon)}</section>${history(payload.firmware,true)}${payload.sourceBuilds?.meta?.page?.total?`<section class="detail-section"><details><summary>AOSP source-build evidence · ${payload.sourceBuilds.meta.page.total}</summary><div id="productSourceBuilds">${sourceBuildsContent(payload.sourceBuilds)}</div></details></section>`:''}<section class="detail-section"><h3>Android upgrades · ${payload.androidUpgrades.length}</h3>${payload.androidUpgrades.map(e=>`<div class="detail-fact"><b>Android ${escapeHtml(e.before.android)} → ${escapeHtml(e.after.android)}</b><small>${escapeHtml(e.after.region)} · ${escapeHtml(e.effective_at)}</small><small>${escapeHtml(e.before.build)} → ${escapeHtml(e.after.build)}</small></div>`).join('')||'<p>No upgrade event captured. Missing Android versions are not inferred from builds or launch specifications.</p>'}</section>${history(payload.security,false)}<p class="subtle">Security patch statements appear only when explicitly provided by the source. Modem/baseband coverage remains incomplete.</p><p class="subtle">Security publications are source statements, not CVE applicability or proof of safety. ROM history contains all captured releases accessible through the pages above; it does not imply complete vendor coverage.</p></div>`);
    const pages = {true:payload.firmware,false:payload.security};
    let historyRequest = 0;
    async function loadHistory(firmware, append) {
      const ticket=++historyRequest, container=$(`#${firmware?'productFirmware':'productPublications'}`);
      const page=pages[firmware], cursor=append?page.meta.page.nextCursor:0;
      try {
        const next=await (firmware?api.productReleases:api.productSecurity)({product:id,limit:50,cursor,
          ...(firmware?{region:$('#detailRegion')?.value||'',channel:$('#detailChannel')?.value||''}:{})});
        if(request!==detailRequest || ticket!==historyRequest)return;
        if(append)next.items=[...page.items,...next.items];
        pages[firmware]=next; container.innerHTML=historyContent(next,firmware); bindHistory();
      } catch { toast('Could not load product history. Please retry.'); }
    }
    function bindHistory(){for(const firmware of [true,false])$(`#${firmware?'productFirmware':'productPublications'} .history-more`)?.addEventListener('click',()=>loadHistory(firmware,true));}
    bindHistory();
    let sourceBuildPage=payload.sourceBuilds;
    function bindSourceBuilds(){ $('#sourceBuildsMore')?.addEventListener('click',async()=>{
      try {
        const next=await api.productSourceBuilds({product:id,limit:50,cursor:sourceBuildPage.meta.page.nextCursor});
        if(request!==detailRequest)return;
        next.items=[...sourceBuildPage.items,...next.items];sourceBuildPage=next;
        $('#productSourceBuilds').innerHTML=sourceBuildsContent(next);bindSourceBuilds();
      } catch {toast('Could not load source-build evidence. Please retry.');}
    });}
    bindSourceBuilds();
    for(const selector of ['#detailRegion','#detailChannel'])$(selector)?.addEventListener('change',()=>loadHistory(true,false));
  } catch {
    if(request===detailRequest)openDetail('<div class="detail-head"><h2>Product unavailable</h2><button class="button detail-close">Close</button></div><div class="detail-body">Could not load this product. Close and retry.</div>');
  }
}
function historyContent(page,firmware){
  if(!page.items.length)return `<div class="notice">No ${firmware?'ROM history':'security publications'} captured for this selection. This is a coverage gap.</div>`;
  return `<p class="subtle">Showing ${page.items.length} of ${page.meta.page.total} captured records</p><div class="data-card"><table class="data-table"><thead><tr>${firmware?'<th>Region / channel</th><th>Build / evidence</th><th>Android</th><th>Released / observed</th>':'<th>Patch statement</th><th>Published</th><th>Publication / source</th>'}</tr></thead><tbody>${productHistoryRows(page.items,firmware)}</tbody></table></div>${page.meta.page.nextCursor?'<button class="button history-more">Load next 50</button>':''}`;
}

function openReviewProfile(id){const p=state.data.reviewProfiles.find(x=>x.id===id);if(!p)return;openDetail(`<div class="detail-head"><div><div class="eyebrow">Review profile · ${escapeHtml(p.tier)}</div><h2>${escapeHtml(p.name)}</h2><div class="subtle">${escapeHtml(p.source_identity.join(' · '))}</div></div><button class="button detail-close">Close</button></div><div class="detail-body"><div class="notice">Source-complete preview, not canonical yet: ${escapeHtml(p.review_state)}.</div><section class="detail-section"><h3>GSMArena specification artifact</h3><div class="detail-grid">${[['Chipset',p.chipset],['CPU',p.cpu],['GPU',p.gpu],['Launch OS',p.launch_os],['Display',p.display],['Battery',p.battery],['Memory',p.memory],['Wi-Fi',p.wifi],['Bluetooth',p.bluetooth],['Observed',p.spec_observed_at]].map(([k,v])=>`<div class="detail-fact"><small>${k}</small><b>${val(v,k)}</b></div>`).join('')}</div></section><section class="detail-section"><h3>Firmware observations</h3><div class="data-card"><table class="data-table"><thead><tr><th>Market/source name</th><th>Codename</th><th>Version</th><th>Android</th><th>Date</th></tr></thead><tbody>${p.firmware.map(x=>`<tr><td>${escapeHtml(x.name)}</td><td>${escapeHtml(x.codename)}</td><td><b>${escapeHtml(x.version)}</b></td><td>${escapeHtml(x.android)}</td><td>${escapeHtml(x.date)}</td></tr>`).join('')}</tbody></table></div></section><section class="detail-section"><h3>Radio</h3><div class="detail-fact"><small>4G</small>${val(p.radio['4g'],'4G bands')}</div><div class="detail-fact"><small>5G</small>${val(p.radio['5g'],'5G bands')}</div></section></div>`);}
async function openSecurityFinding(cve){
  const request=++detailRequest;
  try {
    const x=await api.securityDetail(typeof cve==='string'?cve:cve.cve);
    if(request!==detailRequest)return;
    const provenance=e=>`<div class="subtle">${externalLink(e.source_url,e.source_url_kind==='source_homepage'?'Source website (exact bulletin URL unavailable)':'Captured original source')} · ${escapeHtml(e.source||'Source unknown')} · observed ${val(e.observed_at,'observed at')}<br>${escapeHtml(e.locator||'')}<br>${e.sha256?'SHA-256 '+escapeHtml(e.sha256):'Capture hash unavailable'}</div>`;
    openDetail(`<div class="detail-head"><div><div class="eyebrow">Security evidence chain</div><h2>${escapeHtml(x.cve)}</h2><div class="subtle">${x.published_precision==='month'?'Bulletin month':'Published'} ${escapeHtml(x.published_at||'date unknown')}</div></div><button class="button detail-close">Close</button></div><div class="detail-body"><div class="notice"><b>${escapeHtml(x.state)}</b><br>${escapeHtml(x.reasoning)}</div><div class="toolbar">${externalLink(x.cve_url,'Open CVE record')}</div><section class="detail-section"><h3>Captured bulletins · ${x.bulletins.length}</h3>${x.bulletins.map(e=>`<div class="detail-fact"><b>${escapeHtml(e.title)}</b><small>${e.published_precision==='month'?'Bulletin month':'Published'} ${val(e.published_at,'published date')}</small>${provenance(e)}</div>`).join('')}</section><section class="detail-section"><h3>Applicability claims · ${x.claims.length}</h3>${x.claims.map(e=>`<div class="detail-fact"><b>${escapeHtml(e.relationship)} · ${escapeHtml(e.subject_type)} · ${escapeHtml(e.part_number||e.subject_id)}</b><small>Constraints: ${escapeHtml(JSON.stringify(e.constraint))}</small>${provenance(e)}</div>`).join('')||'<p>No applicability claim captured.</p>'}</section><section class="detail-section"><h3>Reviewed hardware linked through affected parts</h3><p>${escapeHtml(x.boundaries.hardwareLinks)}</p>${x.mappedHardware.map(e=>`<div class="detail-fact"><button class="entity-button" data-device="${escapeHtml(e.model_code)}">${escapeHtml(e.model_code)}</button><small>${escapeHtml(e.part_number)} · ${escapeHtml(e.role)}</small>${provenance(e)}</div>`).join('')||'<p>No exact hardware link captured.</p>'}</section><section class="detail-section"><h3>CVE fix coordinates · ${x.fixes.length}</h3><p>${escapeHtml(x.boundaries.fixCoordinates)}</p>${x.fixes.map(e=>`<div class="detail-fact"><b>${escapeHtml(e.fix_kind)}</b><small>${escapeHtml(JSON.stringify(e.coordinate))}</small>${provenance(e)}</div>`).join('')||'<p>No fix coordinate captured. This does not establish that a device is unfixed.</p>'}</section><section class="detail-section"><h3>Adjudicated verdicts · ${x.verdicts.length}</h3>${x.verdicts.map(e=>`<div class="detail-fact"><b>${escapeHtml(e.status)} · ${escapeHtml(e.subject_type)} · ${escapeHtml(e.subject_id)}</b><small>Rule ${escapeHtml(e.rule_id)} v${escapeHtml(e.rule_version)} · ${escapeHtml(e.evaluated_at)}</small><small>Inputs: ${escapeHtml(JSON.stringify(e.inputs))}</small><details><summary>Verdict evidence summary</summary><p>${escapeHtml(JSON.stringify(e.evidence_summary))}</p></details></div>`).join('')||'<p>No verdict captured. Device and firmware fix state remain unknown.</p>'}</section><p class="subtle">${escapeHtml(x.boundaries.productApplicability)} ${escapeHtml(x.boundaries.missingEvidence)}</p></div>`);
  } catch {if(request===detailRequest)toast('Could not load this CVE. Please retry.');}
}
function panelSecurityLinks(){document.querySelectorAll('#detailPanel .security-inline').forEach(x=>x.addEventListener('click',()=>openSecurityFinding(x.dataset.securityCve)));}

function openDetail(html){const panel=$('#detailPanel'),scrim=$('#detailScrim');panel.innerHTML=html;panel.scrollTop=0;panel.classList.add('open');scrim.classList.add('open');bindEntities(panel);panel.querySelector('.detail-close')?.addEventListener('click',closeDetail);}
function closeDetail(){detailRequest++;$('#detailPanel').classList.remove('open');$('#detailScrim').classList.remove('open');}
function openHelp(){openDetail(`<div class="detail-head"><div><div class="eyebrow">Field guide</div><h2>How Mobile Observatory works</h2><div class="subtle">Evidence first. Conclusions second.</div></div><button class="button detail-close">Close</button></div><div class="detail-body"><section class="detail-section"><h3>1 · Collect</h3><p>Collectors preserve vendor and community artifacts as immutable evidence. <b>Source Records</b> shows what they actually said, including unresolved names.</p></section><section class="detail-section"><h3>2 · Resolve identity</h3><p>A source codename, commercial name, hardware model code, region and chipset are different identities. Known mappings are reused automatically. Uncertain matches enter the <b>Identity review inbox</b> for approve, reject or defer.</p></section><section class="detail-section"><h3>3 · Promote trusted facts</h3><p>Only reviewed or authoritative relationships enter Devices, Canonical ROMs and security relations. An agent may propose a relationship; it cannot silently rewrite canonical facts.</p></section><section class="detail-section"><h3>4 · Detect change</h3><p>New firmware for a known model and region is compared with the previous release and becomes an immutable Radar event. Effective vendor dates and collection times remain separate.</p></section><section class="detail-section"><h3>Reading uncertainty</h3><div class="detail-grid"><div class="detail-fact"><small>Promoted</small><b>Connected to reviewed identity</b></div><div class="detail-fact"><small>Proposed</small><b>Awaiting identity review</b></div><div class="detail-fact"><small>Unknown</small><b>Evidence is incomplete—not proof of absence</b></div><div class="detail-fact"><small>Snapshot</small><b>Portable offline state at a stated time</b></div></div></section></div>`);}
function deviceHistoryContent(page){
  if(!page.items.length)return '<div class="notice">No captured ROMs match this selection. Coverage may be incomplete.</div>';
  return `<p class="subtle">Showing ${page.items.length} of ${page.meta.page.total} captured releases</p><div class="data-card"><table class="data-table"><thead><tr><th>Region / channel</th><th>Build / evidence</th><th>Android / SPL</th><th>Dates</th></tr></thead><tbody>${page.items.map(x=>`<tr><td>${escapeHtml(x.region)}<small>${escapeHtml(x.channel)}</small></td><td><b>${escapeHtml(x.build)}</b><div class="subtle">${externalLink(x.download_url,'Download ROM')} ${externalLink(x.source_url,'Captured manifest / source')}</div><small>Baseband: ${escapeHtml(knownValue(x.baseband))}</small></td><td>${escapeHtml(knownValue(x.android))}<small>SPL: ${escapeHtml(knownValue(x.patch))}</small></td><td>Vendor release: ${escapeHtml(knownValue(x.released))}${x.build_derived_month?'<small>Build-derived month: '+escapeHtml(x.build_derived_month)+' (not release date)</small>':''}<small>First observed: ${escapeHtml(knownValue(x.observed))}</small></td></tr>`).join('')}</tbody></table></div>${page.meta.page.nextCursor?'<button class="button" id="deviceHistoryMore">Load next 50</button>':''}`;
}
async function openDeviceRemote(model){
  const request=++detailRequest;
  openDetail('<div class="detail-head"><h2>Device evidence</h2><button class="button detail-close">Close</button></div><div class="detail-body">Loading captured history…</div>');
  try {
    const payload=await api.deviceDetail(model);
    if(request!==detailRequest)return;
    const d=payload.device;
    let firmware=payload.firmware,security=payload.security,historyRequest=0,securityLoading=false;
    const securityContent=page=>`<p class="subtle">${escapeHtml(payload.boundaries.security)}</p>${page.items.map(x=>`<button class="detail-fact entity-button security-inline" data-security-cve="${escapeHtml(x.cve)}"><b>${escapeHtml(x.cve)}</b><small>${escapeHtml(x.state)}</small></button>`).join('')||'<p>No exact-part security relationship captured.</p>'}${page.meta.page.nextCursor?'<button class="button" id="deviceSecurityMore">Load next 50 findings</button>':''}`;
    openDetail(`<div class="detail-head"><div><div class="eyebrow">Reviewed hardware</div><h2>${escapeHtml(d.variant)}</h2><div class="subtle">${escapeHtml(d.brand)} · ${escapeHtml(d.model_code)}</div></div><button class="button detail-close">Close</button></div><div class="detail-body"><div class="notice">${escapeHtml(payload.boundaries.identity)}</div><section class="detail-section"><details><summary>Hardware aliases and codename</summary><p>Codename: ${escapeHtml(knownValue(d.codename))}</p>${payload.aliases.map(x=>`<div class="detail-fact"><b>${escapeHtml(x.alias)}</b><small>${escapeHtml(x.namespace)} · ${escapeHtml(x.review_state)}</small></div>`).join('')||'<p>No aliases captured.</p>'}</details></section><section class="detail-section"><h3>Reviewed silicon</h3>${payload.silicon.map(x=>`<div class="detail-fact"><button class="entity-button" data-chip="${escapeHtml(x.part_number)}">${escapeHtml(x.marketing_name)} · ${escapeHtml(x.part_number)}</button><small>${escapeHtml(x.role)} · Revision ${escapeHtml(knownValue(x.revision_code))}</small></div>`).join('')||'<p>No reviewed hardware-to-silicon mapping captured.</p>'}</section>${(payload.specifications||[]).length?`<section class="detail-section"><h3>Captured community specifications</h3><p>Exact model appears in a community specification. These assertions do not promote canonical hardware-to-silicon mappings.</p>${payload.specifications.map(x=>`<div class="detail-fact"><b>${escapeHtml(x.name)} · ${val(x.chipset,'chipset')}</b><small>${escapeHtml(x.confidence)} confidence · ${escapeHtml(x.identity_scope)}</small>${externalLink(x.source_url,'Captured specification')}<small>Model list: ${escapeHtml(JSON.stringify(x.models))}</small><small>Observed ${escapeHtml(x.observed_at)} · SHA-256 ${escapeHtml(x.artifact_sha256)}</small></div>`).join('')}</section>`:''}<section class="detail-section"><details><summary>Latest known regional builds · ${(payload.latestFirmware||[]).length}</summary><p>These are snapshot statements, not a live update check. Historical capture order alone is not treated as latest firmware.</p>${(payload.latestFirmware||[]).map(x=>`<div class="detail-fact"><b>${val(x.region,'region')} · ${escapeHtml(x.build)}</b><small>${escapeHtml(x.channel)} · Android ${escapeHtml(knownValue(x.android))} · SPL ${escapeHtml(knownValue(x.patch))}</small><small>${x.latest_basis==='source_manifest_latest'?'Explicit latest in manifest captured '+escapeHtml(x.declared_latest_at):'Latest known vendor release date '+escapeHtml(x.released)}</small></div>`).join('')||'<p>No definite latest firmware established by the captured evidence.</p>'}</details></section><section class="detail-section"><h3>Captured ROM history · ${firmware.meta.page.total}</h3><p class="subtle">${escapeHtml(payload.boundaries.history)} Dates retain their source meaning; build strings are not inferred as Android or security patch levels.</p><div class="toolbar"><select id="deviceRegion" aria-label="Device ROM region"><option value="">All regions</option>${[...new Set(payload.regions.map(x=>x.region).filter(Boolean))].map(x=>`<option>${escapeHtml(x)}</option>`).join('')}</select><select id="deviceChannel" aria-label="Device ROM channel"><option value="">All channels</option>${[...new Set(payload.regions.map(x=>x.channel).filter(Boolean))].map(x=>`<option>${escapeHtml(x)}</option>`).join('')}</select></div><div id="deviceFirmware">${deviceHistoryContent(firmware)}</div></section><section class="detail-section"><h3>Security relationships · ${security.meta.page.total}</h3><div id="deviceSecurity">${securityContent(security)}</div></section></div>`);
    async function loadHistory(append){
      const ticket=++historyRequest;
      try {
        const next=await api.releases({model_exact:d.model_code,region_exact:$('#deviceRegion').value,channel_exact:$('#deviceChannel').value,limit:50,cursor:append?firmware.meta.page.nextCursor:0});
        if(request!==detailRequest||ticket!==historyRequest)return;
        if(append)next.items=[...firmware.items,...next.items];firmware=next;
        $('#deviceFirmware').innerHTML=deviceHistoryContent(firmware);bindHistory();
      }catch{if(request===detailRequest)toast('Could not load device history. Please retry.');}
    }
    function bindHistory(){$('#deviceHistoryMore')?.addEventListener('click',()=>loadHistory(true));}
    function bindSecurity(){panelSecurityLinks();$('#deviceSecurityMore')?.addEventListener('click',async()=>{if(securityLoading)return;securityLoading=true;try{const next=await api.security({model:d.model_code,limit:50,cursor:security.meta.page.nextCursor});if(request!==detailRequest)return;next.items=[...security.items,...next.items];security=next;$('#deviceSecurity').innerHTML=securityContent(security);bindSecurity();}catch{toast('Could not load findings. Please retry.');}finally{securityLoading=false;}});}
    bindHistory();bindSecurity();
    for(const selector of ['#deviceRegion','#deviceChannel'])$(selector).addEventListener('change',()=>loadHistory(false));
  } catch {
    if(request===detailRequest)openDetail('<div class="detail-head"><h2>Device unavailable</h2><button class="button detail-close">Close</button></div><div class="detail-body">Could not load an exact reviewed hardware identity. Product-level names remain available in Product evidence.</div>');
  }
}

async function openChipRemote(part){
  let c=state.data.chips.find(x=>x.part===part);
  const request=++detailRequest;
  try {
    if(!c){c=items(await api.chips({part,limit:1}))[0];if(c)state.data.chips.push(c);}
    if(!c){toast('No captured silicon record found');return;}
    const [payload,linked,security]=await Promise.all([api.devices({part,limit:200}),api.chipProducts({vendor:c.vendor,part,limit:200}),api.security({part,silicon_vendor:c.vendor,limit:50})]);
    c.security=security;
    c.products=items(linked);
    let cursor=linked.meta.page.nextCursor;
    while(cursor){const more=await api.chipProducts({vendor:c.vendor,part,limit:200,cursor});c.products.push(...items(more));cursor=more.meta.page.nextCursor;}
    if(request!==detailRequest)return;
    const previous=state.data.devices; state.data.devices=items(payload); openChip(part); state.data.devices=previous;
    $('#chipAllFindings')?.addEventListener('click',()=>{closeDetail();state.route='security';state.securityFilters={part,silicon_vendor:c.vendor};loadSecurityPage(0).catch(()=>toast('Could not load linked CVEs'));});
  } catch { toast('Could not load the silicon relationships. Please retry.'); }
}
function bindEntities(root=document){bindWatches(root);root.querySelectorAll('[data-product]').forEach(x=>x.addEventListener('click',()=>openProductRemote(x.dataset.product)));root.querySelectorAll('[data-device]').forEach(x=>x.addEventListener('click',()=>openDeviceRemote(x.dataset.device)));root.querySelectorAll('[data-chip]').forEach(x=>x.addEventListener('click',()=>openChipRemote(x.dataset.chip)));}

function renderAgentProposals(){const rows=state.data.agentProposals||[];return `<h2 class="section-title">Agent proposals · ${rows.filter(x=>x.status==='pending').length} pending</h2><div class="data-card flow"><details><summary>Import an agent response</summary><p>Paste the JSON array from the assignment. Imports preserve proposals locally; they never create canonical hardware, aliases, chip mappings, or security claims.</p><textarea id="proposalJson" rows="6" style="width:100%" aria-label="Agent proposal JSON" placeholder="[ { … } ]"></textarea><div class="toolbar"><button class="button primary" id="importProposals">Validate and import proposals</button><label class="button">Read JSON file<input id="proposalFile" type="file" accept="application/json" hidden></label></div></details><p class="subtle">Reviewing remembers the assessment only. Model-code and alias promotion remains a separate evidence review. Identical or previously rejected/deferred targets are not silently reopened.</p>${rows.slice(0,20).map(x=>`<details data-proposal="${escapeHtml(x.id)}"><summary>${escapeHtml(x.proposal.canonical_name)} · ${escapeHtml(x.status)}</summary><p>Agent recommends <b>${escapeHtml(x.proposal.decision)}</b> · ${escapeHtml(x.proposal.confidence)} confidence</p><p>${escapeHtml(x.proposal.rationale)}</p><p>Proposed model codes: ${escapeHtml(x.proposal.model_codes.join(', ')||'none')}<br>Proposed aliases: ${escapeHtml(x.proposal.aliases.join(', ')||'none')}</p>${x.proposal.evidence.map(e=>`<p>${externalLink(e.url,'Evidence URL')} ${escapeHtml(e.note)}<br><small>${escapeHtml(e.artifact_id||e.observation_id||'External reference; not yet captured')}</small></p>`).join('')}${x.reviewed_at?`<p>Remembered ${escapeHtml(x.status)} · ${escapeHtml(x.reviewer)} · ${escapeHtml(x.reviewed_at)}<br>${escapeHtml(x.rationale)}</p>`:''}<label>Review rationale<input class="proposal-rationale" placeholder="Why the evidence supports this decision"></label><div class="toolbar"><button class="button review-proposal" data-decision="same">Accept proposal</button><button class="button review-proposal" data-decision="different">Reject</button><button class="button review-proposal" data-decision="defer">Defer</button></div>${x.reviews.length?`<details><summary>Preserved review history</summary>${x.reviews.map(r=>`<p>${escapeHtml(r.created_at)} · ${escapeHtml(r.decision)} · ${escapeHtml(r.reviewer)}<br>${escapeHtml(r.rationale)}</p>`).join('')}</details>`:''}</details>`).join('')||'<p>No agent proposals imported yet.</p>'}${rows.length>20?'<p>Showing the first 20 proposals; all records remain available through the proposal API.</p>':''}</div>`;}

function renderCollectionJobs(){return `<details><summary>Recent jobs and preserved logs · ${(state.data.collectionRequests||[]).length}</summary>${(state.data.collectionRequests||[]).map(x=>`<div class="detail-fact"><b>#${x.id} · ${escapeHtml(x.target)} · ${escapeHtml(x.status)}</b><small>${escapeHtml(x.source)} · ${escapeHtml(x.scope)}${x.retry_of?` · retry of #${x.retry_of}`:''}</small><small>Requested ${escapeHtml(x.requested_at)} · completed ${escapeHtml(x.finished_at||'—')}</small>${x.result?`<small>${escapeHtml(x.result.limitation||x.result.error||'Captured replay; no live network request.')}</small>`:''}${(x.logs||[]).map(l=>`<small>${escapeHtml(l.at)} · ${escapeHtml(l.level)} · ${escapeHtml(l.message)}</small>`).join('')}${['failed','partial','interrupted'].includes(x.status)?`<button class="button retry-collection" data-id="${x.id}">Queue retry</button>`:''}</div>`).join('')||'<p>No collection jobs yet.</p>'}</details>`;}

function identityMatchRow(hit,query,namespace){
  const remembered=(state.data.decisions||[]).find(d=>d.source_namespace===namespace&&d.source_value===query&&d.canonical_type==='hardware_model'&&d.canonical_id===hit.id);
  const choices=`<div style="margin-top:8px"><button class="button identity-decision" data-choice="same" data-id="${escapeHtml(hit.id)}">Yes, same device</button> <button class="button identity-decision" data-choice="different" data-id="${escapeHtml(hit.id)}">No, different</button> <button class="button identity-decision" data-choice="defer" data-id="${escapeHtml(hit.id)}">Decide later</button></div>`;
  return `<div class="query-match"><b>${escapeHtml(hit.label)}</b><small> ${escapeHtml(hit.detail)} · canonical device</small>${remembered?`<p><b>Remembered: ${escapeHtml(remembered.decision)}</b> · ${escapeHtml(remembered.decided_at)}<br>${escapeHtml(remembered.rationale||'No rationale supplied')}</p><details><summary>Review this remembered decision</summary>${choices}</details>`:choices}</div>`;
}

function renderAdmin() {
  const health=state.data.health.filter(matches), delayed=health.filter(x=>!['healthy','succeeded'].includes(String(x.status).toLowerCase())||x.silent).length;
  const cfg=state.config||{};
  return heading('Operations','Know when the data is trustworthy','Collection health, coverage gaps, and offline snapshot readiness.') + `
  <div class="notice">${delayed} ${delayed===1?'source needs':'sources need'} attention. Product views preserve the last valid observation and display its age.</div>
  <div class="admin-grid"><div><div class="data-card"><table class="data-table"><thead><tr><th>Source</th><th>Scope</th><th>Status</th><th>Evidence captured / imported</th><th>Collection</th><th>Records</th></tr></thead><tbody>${health.map(x=>`<tr><td class="strong">${escapeHtml(x.source)}</td><td>${escapeHtml(x.scope)}</td><td>${badge(x.status,x.status)}${silenceNote(x)}</td><td>${val(x.captured_at,'capture time')}<div class="subtle">Imported ${escapeHtml(x.last)}</div></td><td>${escapeHtml(x.next)}<div class="subtle">${escapeHtml(x.execution_mode||'snapshot')}</div></td><td>${escapeHtml(x.records)}</td></tr>`).join('')}</tbody></table></div></div>
  <div class="data-card flow"><div class="eyebrow">Offline bundle</div><h2 class="section-title" style="margin-top:4px">Snapshot contents</h2><div class="flow-step"><i>1</i><div><b>Corpus database</b><div class="subtle">Canonical facts + provenance</div></div></div><div class="flow-step"><i>2</i><div><b>Evidence cache</b><div class="subtle">Permitted source documents</div></div></div><div class="flow-step"><i>3</i><div><b>Manifest</b><div class="subtle">Source cutoffs, hashes, row counts</div></div></div><p class="subtle">Build and verify snapshots with <code>python3 -m mobile_observatory.snapshots</code>.</p></div></div>
  <h2 class="section-title">Collection preferences</h2><div class="data-card"><div class="form-grid"><label>Preferred cadence (no scheduler installed)<select id="cfgCadence"><option value="3">3 hours</option><option value="6">6 hours</option><option value="12">12 hours</option><option value="24">Daily</option></select></label><label>Catalog scope<select id="cfgSupported"><option value="1">Supported devices only</option><option value="0">All devices</option></select></label></div><div class="validation-note"><b>Preferred regions</b> — validated codes; hover/select labels explain their scope.</div><div class="check-grid" id="cfgRegions">${(state.data.configOptions?.regions||[]).map(x=>`<label class="check-pill"><input type="checkbox" value="${escapeHtml(x.id)}" ${(cfg.preferredRegions||[]).includes(x.id)?'checked':''}> ${escapeHtml(x.id)} · ${escapeHtml(x.label)}</label>`).join('')}</div><div class="validation-note"><b>Enabled sources</b> — only installed source contracts can be selected.</div><div class="check-grid" id="cfgSources">${(state.data.configOptions?.sources||[]).map(x=>`<label class="check-pill"><input type="checkbox" value="${escapeHtml(x.id)}" ${(cfg.enabledSources||[]).includes(x.id)?'checked':''}> ${escapeHtml(x.label)}</label>`).join('')}</div><div class="toolbar"><button class="button primary" id="saveConfig">Save preferences</button><button class="button" id="exportConfig">Export config</button><label class="button">Import config<input id="importConfig" type="file" accept="application/json" hidden></label></div></div>
  <h2 class="section-title">Resolve and query one model</h2><div class="data-card"><div class="form-grid"><label>Model name, code, or alias<input id="modelQuery" placeholder="e.g. SM-S931B or Galaxy S25"></label><label>Query sources<select id="querySource"><option value="all">All enabled sources</option>${(cfg.enabledSources||[]).map(x=>`<option>${escapeHtml(x)}</option>`).join('')}</select></label></div><div id="modelMatch" class="query-match">Enter a model. Atlas will show the canonical identity before any source query is run.</div></div>
  <h2 class="section-title">Identity review inbox · ${state.data.productPage?.total||0} product candidates</h2><div class="notice">Approving remembers the source-to-product relationship and marks all linked history ready for later exact hardware promotion. It does not invent a vendor model code.</div><div class="data-card"><table class="data-table"><thead><tr><th>Product candidate</th><th>Source identities</th><th>History rows</th><th>Specification evidence</th><th>State</th><th>Decision</th></tr></thead><tbody>${productRows(state.data.sourceProducts||[])}</tbody></table></div><div class="toolbar"><button class="button" id="productPrev" ${state.data.productPage?.offset?'':'disabled'}>← Previous</button><span class="subtle">Rows ${(state.data.productPage?.total||0)?state.data.productPage.offset+1:0}-${(state.data.productPage?.offset||0)+(state.data.sourceProducts?.length||0)} of ${state.data.productPage?.total||0}</span><button class="button" id="productNext" ${state.data.productPage?.nextCursor?'':'disabled'}>Next →</button></div>
  <div class="data-card flow"><div class="eyebrow">Agent handoff</div><h2 class="section-title" style="margin-top:4px">Resolve the remaining ${state.data.agentBundle?.candidateCount||0}</h2><p>This bundle includes remaining ambiguous or insufficient cases, the evidence rules, and the JSON proposal contract. Previously reviewed agent targets are remembered. An agent cannot silently change canonical facts.</p><div class="toolbar"><button class="button primary" id="copyAgentPrompt">Copy complete agent assignment</button><button class="button" id="downloadAgentBundle">Download candidate JSON</button></div></div>
  ${renderAgentProposals()}<h2 class="section-title">Remembered identity decisions</h2><div class="data-card">${state.data.decisions?.length?`<table class="data-table"><thead><tr><th>Source value</th><th>Canonical identity</th><th>Decision</th><th>When</th></tr></thead><tbody>${state.data.decisions.map(x=>`<tr><td>${escapeHtml(x.source_namespace)} · ${escapeHtml(x.source_value)}</td><td>${escapeHtml(x.canonical_id||'none')}</td><td>${badge(x.decision,x.decision==='same'?'good':'Unknown')}</td><td>${escapeHtml(x.decided_at)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No decisions yet. Both manual resolution and the local agent will consult this memory before proposing again.</div>'}</div>
  <details class="data-card"><summary>Identity decision history · ${(state.data.identityHistory||[]).length}</summary>${(state.data.identityHistory||[]).slice(0,50).map(x=>`<p>${escapeHtml(x.decided_at)} · ${escapeHtml(x.source_value)} → ${escapeHtml(x.canonical_id||'no target')} · <b>${escapeHtml(x.decision)}</b><br><small>${escapeHtml(x.rationale||'No rationale supplied')} · ${escapeHtml(x.author)}</small></p>`).join('')||'<p>No identity review history.</p>'}</details><h2 class="section-title">Real-source evaluation sample</h2><p class="subtle">${escapeHtml(state.data.realSample?.notice||'')}</p><div class="data-card"><table class="data-table"><thead><tr><th>Vendor / tier</th><th>Source identity</th><th>Region</th><th>Observed value</th><th>Effective</th><th>Source</th></tr></thead><tbody>${(state.data.realSample?.items||[]).map(x=>`<tr><td><b>${escapeHtml(x.vendor)}</b><div class="subtle">${escapeHtml(x.tier)}</div></td><td>${escapeHtml(x.device)}<div class="subtle">${escapeHtml(x.identity)}</div></td><td>${escapeHtml(x.region)}</td><td><b>${escapeHtml(x.value)}</b><div class="subtle">${escapeHtml(x.kind)}</div></td><td>${escapeHtml(x.effective)}</td><td>${escapeHtml(x.source)}</td></tr>`).join('')}</tbody></table></div>
  <h2 class="section-title">Full profiles awaiting identity review</h2><div class="data-card"><table class="data-table"><thead><tr><th>Device</th><th>Tier</th><th>Chipset</th><th>ROMs</th><th>Review state</th></tr></thead><tbody>${(state.data.reviewProfiles||[]).map(x=>`<tr><td><button class="entity-button" data-review-profile="${escapeHtml(x.id)}">${escapeHtml(x.name)}</button><div class="subtle">${escapeHtml(x.source_identity.join(', '))}</div></td><td>${val(x.tier,'tier')}</td><td>${val(x.chipset,'chipset')}</td><td>${x.firmware.length}</td><td>${badge(x.review_state,'Unknown')}</td></tr>`).join('')}</tbody></table></div>
  <p class="subtle" style="margin-top:14px">API boundary: <code>${escapeHtml(API_BASE)}</code>. The UI never reads collection or canonical storage directly.</p>`;
}

function render() {
  if(state.loadError||!state.data){$('#app').innerHTML=`<div class="empty"><h2>Data could not be loaded</h2><p>The local API is unavailable. Your stored evidence has not been replaced.</p><p>${escapeHtml(state.loadError||'Waiting for the API')}</p><button class="button primary" id="retryLoad">Retry loading</button></div>`;$('#retryLoad').addEventListener('click',load);return;}

  const active=document.activeElement, activeId=active?.id, selection=active?.selectionStart;
  function renderWatchlist(){
  const rows = state.data.watchlist || [];
  const head = heading('YOUR WATCHLIST','Everything you are following',
    'Every subject you starred, with where it stands right now. Radar tells you what CHANGED; this tells you what you HAVE.');
  if(!rows.length){
    return head + `<div class="data-card"><div class="empty">
      Nothing watched yet. Press <b>☆ Watch</b> on any device in Radar, Explore or
      Product evidence and it will appear here with its latest firmware.
    </div></div>`;
  }
  const canonical = rows.filter(r=>r.layer==='canonical').length;
  return head + `
  <div class="metrics">
    <div class="metric"><small>Watched</small><strong>${rows.length}</strong><span>subjects you follow</span></div>
    <div class="metric"><small>Reviewed hardware</small><strong>${canonical}</strong><span>identity proven</span></div>
    <div class="metric"><small>Product evidence</small><strong>${rows.length-canonical}</strong><span>identity not yet proven</span></div>
    <div class="metric"><small>With firmware</small><strong>${rows.filter(r=>r.latest_build).length}</strong><span>have a build on record</span></div>
  </div>
  <div class="data-card"><table class="data-table"><thead><tr>
    <th>Device</th><th>Layer</th><th>Latest build</th><th>Region</th><th>Android</th>
    <th>Patch</th><th>Silicon</th><th>Releases</th><th>Last seen</th>
  </tr></thead><tbody>${rows.map(r=>`<tr>
    <td class="strong">${val(r.name,'device name')}<div class="subtle">${val(r.maker,'manufacturer')}${r.model_code?' · '+escapeHtml(r.model_code):''}</div></td>
    <td>${badge(r.layer==='canonical'?'Reviewed':'Evidence', r.layer==='canonical'?'good':'Unknown')}</td>
    <td>${val(r.latest_build,'latest build')}</td>
    <td>${val(r.latest_region,'region')}</td>
    <td>${val(r.latest_android,'Android version')}</td>
    <td>${val(r.latest_patch,'security patch level')}</td>
    <td>${val(r.chipset,'chipset')}</td>
    <td>${r.release_count||0}</td>
    <td>${val(r.latest_seen,'last seen')}</td>
  </tr>`).join('')}</tbody></table></div>`;
}

const renderers={radar:renderRadar, watchlist:renderWatchlist, explore:renderExplore, products:renderProducts, security:renderSecurity, admin:renderAdmin};
  // A view whose data is still in flight must say so. Its arrays are empty at this
  // point, and every one of these tables renders empty as "no matching rows" --
  // which would report a still-loading Explore as an empty catalogue.
  if(state.pending && !CORE_ROUTES.includes(state.route)){
    $('#app').innerHTML=`<div class="data-card"><div class="empty">
      <b>Loading ${escapeHtml(state.route)}…</b>
      <p class="subtle">Radar is ready now; this view needs the rest of the snapshot.</p>
    </div></div><div class="skeleton"></div><div class="skeleton"></div>`;
    $('#crumb').textContent=state.route[0].toUpperCase()+state.route.slice(1);
    document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.route===state.route));
    return;
  }
  if(state.restError && !CORE_ROUTES.includes(state.route)){
    $('#app').innerHTML=`<div class="data-card"><div class="empty">
      <b>This view could not load.</b>
      <p>${escapeHtml(state.restError)}</p>
      <p class="subtle">Your stored evidence has not been changed. Radar still works.</p>
      <button class="button primary" id="retryRest">Retry</button>
    </div></div>`;
    $('#retryRest')?.addEventListener('click',()=>{state.pending=true;state.restError=null;render();loadRest();});
    return;
  }
  if(state.route==='admin') ensureAgentBundle();
  $('#app').innerHTML=renderers[state.route]();
  if(state.route==='explore') {
    $('.filters')?.insertAdjacentHTML('beforeend','<label>Filters<button type="button" class="button reset-filters" id="resetFilters">Reset all</button></label>');
    const sortOptions=state.exploreMode==='silicon'?'<option value="mobile_desc">Mobile-linked first</option><option value="devices_desc">Most devices</option><option value="advisories_desc">Most CVEs</option><option value="name_asc">Name A–Z</option>':state.exploreMode==='devices'?'<option value="latest_desc">Latest firmware first</option><option value="name_asc">Name A–Z</option><option value="android_desc">Android highest first</option>':state.exploreMode==='releases'?'<option value="latest_desc">Newest release first</option><option value="oldest_asc">Oldest release first</option><option value="name_asc">Device A–Z</option><option value="android_desc">Android highest first</option>':'<option value="latest_desc">Newest evidence first</option><option value="oldest_asc">Oldest evidence first</option><option value="source_asc">Source A–Z</option><option value="name_asc">Identity A–Z</option>';
    $('#exportView')?.insertAdjacentHTML('beforebegin',`<label class="inline-sort">Sort <select id="exploreSort">${sortOptions}</select></label>`);
  }
  if(state.route==='admin') {
    const unresolved=state.data.productPage?.total||0, chips=state.data.chipPage?.total||0;
    $('#app .notice').innerHTML=`<b>Collector operation:</b> ${state.data.health.filter(x=>!['healthy','succeeded'].includes(String(x.status).toLowerCase())||x.silent).length} source failures (includes advisory silence -- see the Status column). <b>Coverage debt:</b> ${state.data.productPage?.total||unresolved} unresolved product candidates; ${chips} canonical silicon parts; ${state.data.securityPage?.total||state.data.security.length} catalogued CVEs awaiting device applicability. A successful collector run does not mean coverage is complete.`;
    $('#app .admin-grid')?.insertAdjacentHTML('afterend',`<h2 class="section-title">Collect something now</h2><div class="data-card"><div class="form-grid"><label>Target hint<input id="collectionTarget" placeholder="e.g. SM-A055F / ILO or Redmi Note 14"><small>This is a matching hint for preserved artifacts, not an AI prompt or a live web query.</small></label><label>What to collect<select id="collectionScope"><option value="smart">Smart choice for each source</option><option value="latest_firmware">Latest firmware</option><option value="firmware_history">Firmware history</option><option value="device_profile">Device profile and silicon</option><option value="security">Security bulletins</option></select></label></div><div class="validation-note"><b>Sources (choose one or more)</b> — only installed captured-replay combinations are offered.</div><div class="check-grid" id="collectionSources"><label class="check-pill"><input type="checkbox" value="samsung"> Samsung FOTA</label><label class="check-pill"><input type="checkbox" value="xiaomi"> Xiaomi firmware</label><label class="check-pill"><input type="checkbox" value="tecno"> TECNO security</label></div><div class="toolbar"><button class="button primary" id="queueCollection">Queue collection request(s)</button><button class="button" id="runNextCollection">Run next captured replay</button><button class="button" id="recoverCollections">Recover interrupted jobs</button><span class="subtle">Smart maps Samsung→firmware history, Xiaomi→firmware history, TECNO→security. Incompatible explicit combinations are rejected before queueing.</span></div>${renderCollectionJobs()}${(state.data.collectionRequests||[]).length?`<div class="validation-note">Recent: ${(state.data.collectionRequests||[]).slice(0,3).map(x=>`${escapeHtml(x.target)} · ${escapeHtml(x.source)} · ${escapeHtml(x.scope)} · ${escapeHtml(x.status)}`).join(' | ')}</div>`:''}</div>`);
  }
  $('#crumb').textContent=state.route[0].toUpperCase()+state.route.slice(1);
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.route===state.route));
  document.querySelectorAll('.acknowledge').forEach(button=>button.addEventListener('click', async()=>{
    const id=button.dataset.id; state.acknowledged.add(id); render();
    if(!state.fixtureMode) try { await api.acknowledge(id); } catch { state.acknowledged.delete(id); render(); toast('Could not save acknowledgement'); return; }
    state.data.overview=await api.overview();$('#navCount').textContent=state.data.overview.unseen;await loadRadarPage(0);toast('Update marked as seen');
  }));
  ['radarRows','exploreRows'].forEach(id=>{
    const el=$('#'+id); if(!el) return;
    el.value=String(pageSize());
    el.addEventListener('change',async()=>{
      setPageSize(el.value);
      try{
        if(state.route==='radar') await loadRadarPage(0);
        else if(state.route==='explore') await (state.exploreMode==='sources'?loadSourcePage(0):loadCanonicalPage(state.exploreMode,0));
        else if(state.route==='products') await loadProductEvidencePage(0);
        else if(state.route==='security') await loadSecurityPage(0);
        else render();
      }catch{toast('Could not change rows per page');}
    });
  });
  const bindSelect=(id,key)=>{const el=$(`#${id}`);if(!el)return;if(el.tagName==='SELECT')el.value=state[key];const apply=async()=>{state[key]=el.value||'all';if(state.route==='radar')await loadRadarPage(0);else if(state.route==='explore'&&state.exploreMode!=='sources')await loadCanonicalPage(state.exploreMode,0);else render();};if(el.tagName==='INPUT'){el.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();apply().catch(()=>toast('Could not apply filter'));}});el.addEventListener('change',()=>apply().catch(()=>toast('Could not apply filter')));}else el.addEventListener('change',()=>apply().catch(()=>toast('Could not apply filter')));};
  bindSelect('radarRegion','radarRegion'); bindSelect('radarChange','radarChange');
  bindSelect('makerFilter','maker'); bindSelect('chipFilter','chipVendor'); bindSelect('familyFilter','chipFamily'); bindSelect('partFilter','chipPart'); bindSelect('androidFilter','android'); bindSelect('regionFilter','region'); bindSelect('supportFilter','support');
  $('#resetFilters')?.addEventListener('click',()=>{Object.assign(state,{filter:'',maker:'all',chipVendor:'all',chipFamily:'all',chipPart:'all',android:'all',region:'all',support:'all'});searchBox.value='';if(state.exploreMode==='sources')render();else loadCanonicalPage(state.exploreMode,0).catch(()=>render());});
  $('#radarTabs')?.querySelectorAll('button').forEach(x=>x.addEventListener('click',()=>{state.radarTab=x.dataset.value;loadRadarPage(0).catch(()=>toast('Could not load Radar'));}));
  $('#exploreTabs')?.querySelectorAll('button').forEach(x=>x.addEventListener('click',()=>{state.exploreMode=x.dataset.value;if(state.exploreMode==='sources')loadSourcePage(0).catch(()=>render());else loadCanonicalPage(state.exploreMode,0).catch(()=>render());}));
  if($('#exploreSort')){$('#exploreSort').value=state.exploreSort[state.exploreMode];$('#exploreSort').addEventListener('change',e=>{state.exploreSort[state.exploreMode]=e.target.value;if(state.exploreMode==='sources')loadSourcePage(0).catch(()=>toast('Could not sort records'));else loadCanonicalPage(state.exploreMode,0).catch(()=>toast('Could not sort view'));});}
  $('#productEvidenceTabs')?.querySelectorAll('button').forEach(x=>x.addEventListener('click',()=>{state.productMode=x.dataset.value;loadProductEvidencePage(0).catch(()=>toast('Could not load product evidence'));}));
  const productEvidencePage=state.data[state.productMode==='firmware'?'productReleasePage':'productSecurityPage']||{};
  if($('#productMaker'))$('#productMaker').value=state.productMaker;if($('#productSort'))$('#productSort').value=state.productSort;
  $('#securityFilters')?.addEventListener('submit',event=>{event.preventDefault();state.securityFilters={silicon_vendor:state.securityFilters.silicon_vendor||'',q:$('#securityQuery').value.trim(),vendor:$('#securityVendor').value.trim(),date_from:$('#securityFrom').value,date_to:$('#securityTo').value,part:$('#securityPart').value.trim(),model:$('#securityModel').value.trim(),fix_status:$('#securityFix').value,sort:$('#securitySort').value,mobile_linked:$('#securityMobile').checked?'1':'',exact_part:$('#securityExact').checked?'1':''};loadSecurityPage(0).catch(()=>toast('Could not filter security evidence'));});
  $('#securityReset')?.addEventListener('click',()=>{state.securityFilters={};loadSecurityPage(0).catch(()=>toast('Could not reset security evidence'));});
  $('#securityPrev')?.addEventListener('click',()=>loadSecurityPage(Math.max(0,(state.data.securityPage.offset||0)-100)).catch(()=>toast('Could not load page')));
  $('#securityNext')?.addEventListener('click',()=>loadSecurityPage(Number(state.data.securityPage.nextCursor)).catch(()=>toast('Could not load page')));
  $('#productSearch')?.addEventListener('click',()=>{state.productQuery=$('#productQuery').value.trim();state.productMaker=$('#productMaker').value;state.productRegion=$('#productRegion')?.value.trim()||'';state.productSort=$('#productSort').value;loadProductEvidencePage(0).catch(()=>toast('Could not filter product evidence'));});
  $('#productQuery')?.addEventListener('keydown',e=>{if(e.key==='Enter')$('#productSearch').click();});
  $('#productReset')?.addEventListener('click',()=>{Object.assign(state,{productQuery:'',productMaker:'',productRegion:'',productSort:'released_desc'});loadProductEvidencePage(0).catch(()=>toast('Could not reset product evidence'));});
  document.querySelectorAll('.research-product').forEach(button=>button.addEventListener('click',async()=>{try{const queued=await api.requestCollection({target:button.dataset.target,source:button.dataset.source,scope:button.dataset.scope});state.data.collectionRequests.unshift(queued);toast('Evidence research request queued in Admin');}catch{toast('This evidence collector is not available');}}));
  $('#productEvidencePrev')?.addEventListener('click',()=>loadProductEvidencePage(Math.max(0,(productEvidencePage.offset||0)-100)).catch(()=>toast('Could not load page')));
  $('#productEvidenceNext')?.addEventListener('click',()=>{if(productEvidencePage.nextCursor)loadProductEvidencePage(Number(productEvidencePage.nextCursor)).catch(()=>toast('Could not load page'));});
  if($('#sourceName')){$('#sourceName').value=state.sourceName;$('#sourceKind').value=state.sourceKind;}
  $('#sourceSearch')?.addEventListener('click',async()=>{state.sourceQuery=$('#sourceQuery').value.trim();state.sourceName=$('#sourceName').value;state.sourceKind=$('#sourceKind').value;try{await loadSourcePage(0);}catch{toast('Could not query source records');}});
  $('#sourceQuery')?.addEventListener('keydown',e=>{if(e.key==='Enter')$('#sourceSearch').click();});
  $('#sourcePrev')?.addEventListener('click',()=>loadSourcePage(Math.max(0,(state.data.sourcePage?.offset||0)-100)).catch(()=>toast('Could not load page')));
  $('#sourceNext')?.addEventListener('click',()=>loadSourcePage(Number(state.data.sourcePage?.nextCursor)).catch(()=>toast('Could not load page')));
  const canonicalPage=state.data[state.exploreMode==='devices'?'devicePage':state.exploreMode==='silicon'?'chipPage':'releasePage']||{};
  $('#canonicalPrev')?.addEventListener('click',()=>loadCanonicalPage(state.exploreMode,Math.max(0,(canonicalPage.offset||0)-100)).catch(()=>toast('Could not load page')));
  $('#canonicalNext')?.addEventListener('click',()=>{if(canonicalPage.nextCursor)loadCanonicalPage(state.exploreMode,Number(canonicalPage.nextCursor)).catch(()=>toast('Could not load page'));});
  $('#productPrev')?.addEventListener('click',()=>loadProductPage(Math.max(0,(state.data.productPage?.offset||0)-100)).catch(()=>toast('Could not load candidates')));
  $('#productNext')?.addEventListener('click',()=>loadProductPage(Number(state.data.productPage?.nextCursor)).catch(()=>toast('Could not load candidates')));
  document.querySelectorAll('.product-review').forEach(button=>button.addEventListener('click',async()=>{try{await api.reviewSourceProduct(button.dataset.id,button.dataset.decision);await loadProductPage(state.data.productPage?.offset||0);toast(`Product ${button.dataset.decision}`);}catch{toast('Could not save product decision');}}));
  $('#proposalFile')?.addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;if(file.size>1000000){toast('Proposal file must be at most 1 MB');return;}$('#proposalJson').value=await file.text();});
  $('#importProposals')?.addEventListener('click',async()=>{try{const value=JSON.parse($('#proposalJson').value);const result=await api.importAgentProposals(value);await load();toast(`${result.count} proposals validated; canonical facts unchanged`);}catch{toast('Invalid proposal JSON or evidence references; check the assignment contract');}});
  document.querySelectorAll('.review-proposal').forEach(button=>button.addEventListener('click',async()=>{const row=button.closest('[data-proposal]'),rationale=row.querySelector('.proposal-rationale').value.trim();if(!rationale){toast('Add a review rationale');return;}try{await api.reviewAgentProposal(row.dataset.proposal,{decision:button.dataset.decision,rationale});await load();toast('Review remembered; canonical facts unchanged');}catch{toast('Could not record proposal review');}}));
  $('#copyAgentPrompt')?.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(state.data.agentBundle?.pastePrompt||'');toast('Agent assignment copied');}catch{toast('Clipboard unavailable; download the bundle instead');}});
  $('#downloadAgentBundle')?.addEventListener('click',()=>downloadJson('mobile-observatory-identity-candidates.json',state.data.agentBundle?.candidates||[]));
  $('#radarPrev')?.addEventListener('click',()=>loadRadarPage(Math.max(0,(state.data.updatePage?.offset||0)-50)).catch(()=>toast('Could not load Radar page')));
  $('#radarNext')?.addEventListener('click',()=>loadRadarPage(Number(state.data.updatePage?.nextCursor||0)).catch(()=>toast('Could not load Radar page')));
  $('#markAll')?.addEventListener('click',async()=>{const pending=state.data.updates.filter(x=>!state.acknowledged.has(x.id));try{if(!state.fixtureMode)await api.acknowledgeMany(pending.map(x=>x.id));pending.forEach(x=>state.acknowledged.add(x.id));state.data.overview=state.fixtureMode?{...state.data.overview,unseen:Math.max(0,state.data.overview.unseen-pending.length)}:await api.overview();$('#navCount').textContent=state.data.overview.unseen;await loadRadarPage(0);toast(`${pending.length} updates marked as seen`);}catch{toast('Could not save all acknowledgements');}});
  $('#exportView')?.addEventListener('click',()=>{
    const rows=state.exploreMode==='devices'?state.data.devices:state.exploreMode==='silicon'?state.data.chips:state.exploreMode==='sources'?state.data.sourceRecords:state.data.releases;
    const keys=Object.keys(rows[0]||{}), csv=[keys.join(','),...rows.filter(matches).map(row=>keys.map(k=>`"${String(row[k]??'').replaceAll('"','""')}"`).join(','))].join('\n');
    const link=document.createElement('a');link.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));link.download=`observatory-${state.exploreMode}.csv`;link.click();URL.revokeObjectURL(link.href);toast('CSV exported');
  });
  if($('#cfgCadence')){$('#cfgCadence').value=String(state.config.cadenceHours);$('#cfgSupported').value=state.config.supportedOnly?'1':'0';}
  $('#saveConfig')?.addEventListener('click',async()=>{const checked=id=>[...$(`#${id}`).querySelectorAll('input:checked')].map(x=>x.value);const c={cadenceHours:+$('#cfgCadence').value,preferredRegions:checked('cfgRegions'),enabledSources:checked('cfgSources'),supportedOnly:$('#cfgSupported').value==='1'};try{state.config=state.fixtureMode?c:await api.saveConfig(c);toast('Preferences saved');}catch{toast('Configuration contains an unavailable region or source');}});
  $('#queueCollection')?.addEventListener('click',async()=>{const target=$('#collectionTarget').value.trim(),chosen=[...$('#collectionSources').querySelectorAll('input:checked')].map(x=>x.value),requested=$('#collectionScope').value,smart={samsung:'firmware_history',xiaomi:'firmware_history',tecno:'security'},allowed={samsung:['latest_firmware','firmware_history'],xiaomi:['latest_firmware','firmware_history','device_profile'],tecno:['security']};if(!target){toast('Enter a target hint');$('#collectionTarget').focus();return;}if(!chosen.length){toast('Choose at least one source');return;}const jobs=chosen.map(source=>({target,source,scope:requested==='smart'?smart[source]:requested}));if(jobs.some(x=>!allowed[x.source].includes(x.scope))){toast('One selected source cannot collect that evidence type; use Smart');return;}try{for(const value of jobs){const queued=await api.requestCollection(value);state.data.collectionRequests.unshift(queued);}render();toast(`${jobs.length} collection request${jobs.length===1?'':'s'} queued`);}catch{toast('Could not queue all collection requests');}});
  $('#recoverCollections')?.addEventListener('click',async()=>{try{const result=await api.recoverCollections();await load();toast(`${result.interrupted.length} interrupted jobs recovered; retry explicitly`);}catch{toast('A collection worker is active; try again when it finishes');}});
  document.querySelectorAll('.retry-collection').forEach(button=>button.addEventListener('click',async()=>{try{await api.retryCollection(button.dataset.id);await load();toast('Retry queued; original logs preserved');}catch{toast('This request cannot be retried now');}}));
  $('#runNextCollection')?.addEventListener('click',async()=>{try{const result=await api.processNextCollection();await load();toast(result.status==='idle'?'No queued requests':`Captured replay ${result.status}`);}catch{toast('Could not execute collection request');}});
  $('#exportConfig')?.addEventListener('click',()=>downloadJson('mobile-observatory-config.json',state.config));
  $('#importConfig')?.addEventListener('change',async e=>{try{const c=JSON.parse(await e.target.files[0].text());state.config=state.fixtureMode?c:await api.saveConfig(c);render();toast('Configuration imported');}catch{toast('Invalid configuration file');}});
  $('#modelQuery')?.addEventListener('input',debounce(async e=>{const q=e.target.value.trim(),box=$('#modelMatch');if(q.length<2){box.textContent='Enter at least two characters.';return;}try{const hits=items(await api.search(q)).filter(x=>x.type==='device');box.innerHTML=hits.length?hits.map(x=>identityMatchRow(x,q,$('#querySource').value)).join(''):'No canonical model matched. Create a review proposal rather than guessing.';box.querySelectorAll('.identity-decision').forEach(b=>b.onclick=async()=>{const value={sourceNamespace:$('#querySource').value,sourceValue:q,canonicalType:'hardware_model',canonicalId:b.dataset.id,decision:b.dataset.choice,rationale:'manual Admin resolution'};try{await api.saveIdentityDecision(value);state.data.decisions=items(await api.identityDecisions());render();toast('Identity decision remembered');}catch{toast('Could not save identity decision');}});}catch{box.textContent='Search unavailable.';}},180));
  bindEntities($('#app'));
  $('#app').querySelectorAll('.security-detail').forEach(x=>x.addEventListener('click',()=>openSecurityFinding(x.dataset.securityCve)));
  $('#app').querySelectorAll('[data-review-profile]').forEach(x=>x.addEventListener('click',()=>openReviewProfile(x.dataset.reviewProfile)));
  if(activeId){const restored=document.getElementById(activeId);if(restored){restored.focus();if(selection!=null&&restored.setSelectionRange)restored.setSelectionRange(selection,selection);}}
}

function debounce(fn,wait){let timer;return(...args)=>{clearTimeout(timer);timer=setTimeout(()=>fn(...args),wait);};}
function downloadJson(name,value){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));a.download=name;a.click();URL.revokeObjectURL(a.href);}

document.querySelectorAll('[data-route]').forEach(button=>button.addEventListener('click',()=>{if(!state.data)return;state.route=button.dataset.route;searchResults?.classList.remove('open');$('.rail').classList.remove('open');render();}));
const searchBox=$('#globalSearch'),searchResults=$('#searchResults');
searchBox.addEventListener('input',debounce(async event=>{state.filter=event.target.value;if(state.route==='radar'){try{await loadRadarPage(0);}catch{toast('Could not search Radar');}}else render();const q=event.target.value.trim();if(q.length<2){searchResults.classList.remove('open');return;}try{const hits=items(await api.search(q));searchResults.innerHTML=hits.length?hits.map((x,i)=>`<button class="search-hit" data-hit="${i}" data-type="${escapeHtml(x.type)}" data-label="${escapeHtml(x.label)}"><b>${escapeHtml(x.label)}</b><small>${escapeHtml(x.type)} · ${escapeHtml(x.detail)}</small></button>`).join(''):'<div class="empty">No canonical match. Coverage may be incomplete.</div>';searchResults.classList.add('open');searchResults.querySelectorAll('[data-hit]').forEach(b=>b.onclick=()=>{state.filter=b.dataset.label;searchBox.value=b.dataset.label;state.route=b.dataset.type==='release'?'explore':b.dataset.type==='chip'?'explore':'explore';state.exploreMode=b.dataset.type==='release'?'releases':b.dataset.type==='chip'?'silicon':'devices';searchResults.classList.remove('open');render();});}catch{searchResults.innerHTML='<div class="empty">Search unavailable</div>';searchResults.classList.add('open');}},180));
const theme=$('#themeSelect');theme.value=localStorage.getItem('observatory-theme')||'system';const applyTheme=v=>{document.documentElement.dataset.theme=v==='system'?'':v;localStorage.setItem('observatory-theme',v);};applyTheme(theme.value);theme.addEventListener('change',()=>applyTheme(theme.value));
$('#refreshButton').addEventListener('click',()=>load().then(()=>toast('Data refreshed')));
$('#helpButton').addEventListener('click',openHelp);
$('.mobile-menu').addEventListener('click',()=>$('.rail').classList.toggle('open'));
$('#detailScrim').addEventListener('click',closeDetail);
document.addEventListener('keydown',event=>{if(event.key==='/'&&document.activeElement.tagName!=='INPUT'){event.preventDefault();$('#globalSearch').focus();}});
load();

// ===========================================================================
// GAP -> RESEARCH BRIEF
//
// Ray: "i want to be able to click in unavailable info i encounter and copy
// prompt to claude lanched in the pwd and tell him to fix, fill the gaps via
// free web search but he must work according the conventions and restrictions
// we have here".
//
// Design note: the brief is generated from the DOM at click time -- the row's
// identity cell and the column header -- so no rendering template has to pass
// context down. That keeps this feature to one place instead of 60.
//
// The restrictions below are not decoration. Each one is here because ignoring
// it has already cost this project something: a ban, a re-fetch of data we
// already held, or a number that looked measured and was not.
// ===========================================================================

const BRIEF_RESTRICTIONS = `HARD RESTRICTIONS -- these are not negotiable and not stylistic.

ACCESS
- robots.txt is binding even when a fetch looks trivial. Cheapness is not permission.
- Never create an account, enter a password, or solve a CAPTCHA / Turnstile / any
  bot check. If a source needs a login, STOP and report it as login-gated.
- samfw.com is OFF LIMITS (it serves cf-mitigated: challenge).
- deviceinfohw.ru is DISABLED: it 403s honest crawler user-agents. Sending a browser
  UA to get past that would misrepresent what we are. Do not.
- dl.google.com is OFF LIMITS (robots.txt Disallow: /).
- Do not bulk-harvest an archive to route around an active ban. A per-page archive
  lookup as a LAST RESORT, after a direct fetch has already failed, is acceptable.
- Respect a per-host per-day politeness budget. A 429 is the SUM of every run today,
  not just yours. Never delete existing data before a fetch -- if you are blocked,
  deleting first destroys the corpus you were trying to improve.

EVIDENCE
- Authority is a strict order, never an average:
  vendor-official > vendor-derived > community. A community source NEVER overrides
  a vendor one; record it as a conflict instead.
- Every claim needs a retrievable URL, a VERBATIM quote from that page, and a named
  attribute (model_code / chipset / release_date / display / patch_level). Prose
  agreement between two pages is not evidence. Two sources independently landing on
  the same model code is.
- A source merely spelling a name the same way settles nothing.

PRECISION -- the traps that have actually bitten this corpus
- NEVER pad a month to a day. 2026-08 must not become 2026-08-01. The day carries
  Google's -01 / -05 patch tier, and inventing one silently asserts a tier we never
  observed.
- NEVER compare dates across vendors to decide whether something is fixed. Join on
  the CVE ID.
- A real Android patch-level column lands only on day 01 or 05. If you collect one
  and the days are scattered, you have collected RELEASE dates, not patch levels.
- Absence is a measurement and must be recordable. "Not found" is a real, useful
  answer -- write it down with where you looked. Do not leave a blank that reads as
  "not checked yet", and do not convert absence into a negative claim.

OUTPUT BOUNDARY
- Your output is a PROPOSAL. Never write canonical tables directly
  (source_products, observations, identity_conclusions, product_firmware_releases,
  observed_product_silicon). Write to the proposal queue and let the existing
  review/promotion boundary decide.
- Do not invent a model code, a chipset, or a patch level to fill a blank. An
  unresolved gap is a correct outcome.
- If you cannot settle it, say CANNOT_DETERMINE and state what would settle it.`;

function briefFor({ subject, field, column, view, sub }) {
  const who = subject || '(the row you clicked)';
  const what = field || column || 'the missing value';
  return `You are working in the Mobile Observatory repo. Your working directory is
already the project root -- run things from here.

TASK
Fill one specific gap in the corpus, using free web search.

  Subject : ${who}${sub ? `\n  Detail  : ${sub}` : ''}
  Missing : ${what}
  Seen in : the "${view}" view${column && column !== what ? `, column "${column}"` : ''}

The UI renders this value as an em dash, which means NOTHING WAS CAPTURED for it --
it does not mean the value is empty, and it does not mean nobody has looked.

WHAT TO DO
1. FIRST check what we already hold. A re-fetch is the most expensive possible
   answer, so it deserves the most scrutiny before you choose it. Grep the captured
   artifacts under crawler/relay/results/ and the corpus at
   .observatory-data/corpus.sqlite before you touch the network. A previous
   escalation was dispatched to two agents and then answered by a CSV already
   sitting in our own results directory.
2. If that does not settle it, search the web. Prefer the vendor's own publication
   over any aggregator.
3. Record the finding as a proposal with URL + verbatim quote + named attribute.
4. If the answer is "no source publishes this", record THAT, with where you looked.
   A measured negative is a real result and stops the question being re-asked.

${BRIEF_RESTRICTIONS}

RETURN
  VERDICT   : FOUND | NOT_PUBLISHED | LOGIN_GATED | CANNOT_DETERMINE
  VALUE     : the value, at the precision the source actually stated
  AUTHORITY : vendor-official | vendor-derived | community
  EVIDENCE  : url + verbatim quote + attribute
  NOTE      : anything that surprised you, and anything you had to leave undone`;
}

// --- dialog ---------------------------------------------------------------
function ensureBriefDom() {
  if (document.getElementById('briefScrim')) return;
  const scrim = document.createElement('div');
  scrim.className = 'brief-scrim';
  scrim.id = 'briefScrim';
  document.body.appendChild(scrim);

  const box = document.createElement('div');
  box.className = 'brief';
  box.id = 'briefBox';
  box.style.display = 'none';
  box.innerHTML = `
    <header>
      <div>
        <h2>Research this gap</h2>
        <p id="briefSubtitle"></p>
      </div>
      <button class="button detail-close" id="briefClose" aria-label="Close">Close</button>
    </header>
    <div class="brief-body"><textarea id="briefText" spellcheck="false" readonly></textarea></div>
    <div class="brief-actions">
      <button class="button primary" id="briefCopy">Copy prompt</button>
      <button class="button" id="briefCopyCd">Copy with cd + claude</button>
      <span class="spacer"></span>
      <span class="subtle" id="briefHint">Paste into a Claude session started in this repo.</span>
    </div>`;
  document.body.appendChild(box);

  const close = () => { box.style.display = 'none'; scrim.classList.remove('open'); };
  scrim.addEventListener('click', close);
  box.querySelector('#briefClose').addEventListener('click', close);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

  const copy = async (text, btn) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // clipboard API needs a secure context; this is served over plain http on
      // localhost, which usually qualifies -- but fall back rather than fail quietly.
      const ta = box.querySelector('#briefText');
      ta.removeAttribute('readonly'); ta.select();
      document.execCommand('copy');
      ta.setAttribute('readonly', '');
    }
    const was = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = was; }, 1400);
  };
  box.querySelector('#briefCopy').addEventListener('click', e =>
    copy(box.querySelector('#briefText').value, e.currentTarget));
  box.querySelector('#briefCopyCd').addEventListener('click', e => {
    const prompt = box.querySelector('#briefText').value;
    copy(`cd ~/work/git/gsmarena-unblock-merge/mobile-observatory && claude <<'BRIEF'\n${prompt}\nBRIEF`,
      e.currentTarget);
  });
}

function openBrief(context) {
  ensureBriefDom();
  const box = document.getElementById('briefBox');
  document.getElementById('briefSubtitle').textContent =
    `${context.subject || 'row'} - ${context.field || context.column || 'value'}`;
  document.getElementById('briefText').value = briefFor(context);
  box.style.display = '';
  document.getElementById('briefScrim').classList.add('open');
  document.getElementById('briefCopy').focus();
}

// --- context from the DOM -------------------------------------------------
function contextForGap(btn) {
  const cell = btn.closest('td');
  const row = btn.closest('tr');
  const table = btn.closest('table');
  let column = '';
  if (cell && row && table) {
    const idx = [...row.children].indexOf(cell);
    const th = table.querySelectorAll('thead th')[idx];
    if (th) column = th.textContent.trim();
  }
  // the identity of a row lives in its first cell; strip the sub-line separator
  // the one-line CSS introduces so the subject reads as a name, not a blob
  let subject = '', sub = '';
  if (row) {
    const first = row.querySelector('td');
    if (first) {
      const strong = first.querySelector('.strong, b, .entity-button, .link');
      subject = (strong ? strong.textContent : first.textContent).trim();
      const subtle = first.querySelector('.subtle');
      if (subtle) sub = subtle.textContent.replace(/^·\s*/, '').trim();
    }
  }
  if (!subject) {
    const card = btn.closest('.update-card, .detail-fact, .metric');
    if (card) subject = (card.querySelector('h3, small, b') || card).textContent.trim().slice(0, 80);
  }
  return {
    subject, sub, column,
    field: btn.dataset.field || '',
    view: (document.getElementById('crumb') || {}).textContent || 'the app',
  };
}

document.addEventListener('click', e => {
  const btn = e.target.closest('.gap');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();          // a gap sits inside rows that open a detail panel
  openBrief(contextForGap(btn));
});

// --- keep one-line rows informative ---------------------------------------
// The CSS collapses each row to a single line with an ellipsis. Anything that
// overflows would otherwise be lost, so give every truncated cell a title. Run
// it after any re-render rather than hooking a specific render function.
function titleOverflowingCells() {
  document.querySelectorAll('.data-table td').forEach(td => {
    if (td.scrollWidth > td.clientWidth + 1) {
      const text = td.textContent.replace(/\s+/g, ' ').trim();
      if (text && td.title !== text) td.title = text;
    } else if (td.title) {
      td.removeAttribute('title');
    }
  });
}
const appRoot = document.getElementById('app');
if (appRoot) {
  let pending = null;
  new MutationObserver(() => {
    clearTimeout(pending);
    pending = setTimeout(titleOverflowingCells, 60);
  }).observe(appRoot, { childList: true, subtree: true });
}
window.addEventListener('resize', () => {
  clearTimeout(window.__titleTimer);
  window.__titleTimer = setTimeout(titleOverflowingCells, 150);
});

// ===========================================================================
// DEEP-LINKABLE VIEWS
//
// The app had no URL routing: every view lived at "/", so a refresh always
// dumped you back on Radar and no view could be bookmarked, shared, or reopened
// where you left it. This adds a hash route without touching the existing
// navigation -- it drives the same buttons the rail does, so there is exactly
// one code path that changes a view.
//
// The nav handler starts with `if(!state.data)return;`, so a click before the
// first load silently does nothing. That is why applying a hash has to retry
// until the button actually goes active rather than firing once on load.
// ===========================================================================
const ROUTES = ['radar', 'watchlist', 'explore', 'products', 'security', 'admin'];
const routeFromHash = () => {
  const h = (location.hash || '').replace(/^#/, '').toLowerCase();
  return ROUTES.includes(h) ? h : null;
};

// The requested route is captured ONCE, at load, and never re-read from the URL
// while we are still trying to reach it.
//
// The first version re-read the hash on every retry, and lost a race it could
// not win: the app boots on 'radar', writes "Radar" into #crumb, and the
// sync-out observer below rewrote the URL to #radar -- so the next retry read
// #radar and obediently went there. Opening /#security reliably landed on the
// default view. Latching the target and gating the writer is what breaks the
// loop; the two halves must not both own the hash at the same time.
let pendingRoute = routeFromHash();

(function applyPendingRoute(tries = 0) {
  if (!pendingRoute) return;
  const btn = document.querySelector(`.rail button[data-route="${pendingRoute}"]`);
  if (btn && btn.classList.contains('active')) { pendingRoute = null; return; }
  // the rail's own handler starts with `if(!state.data)return;`, so a click
  // before the first load is a no-op rather than an error -- hence retrying
  // until the button actually goes active instead of firing once.
  btn?.click();
  if (btn?.classList.contains('active')) { pendingRoute = null; return; }
  if (tries < 60) setTimeout(() => applyPendingRoute(tries + 1), 250);
  else pendingRoute = null;   // give up rather than spin forever
})();

// Keep the hash in step with the view. #crumb is rewritten on every render and
// holds the route name, so it is the one signal guaranteed to fire for every
// navigation -- including those from search results and the chip findings link.
const crumbEl = document.getElementById('crumb');
if (crumbEl) {
  new MutationObserver(() => {
    if (pendingRoute) return;            // do not fight the route being applied
    const r = crumbEl.textContent.trim().toLowerCase();
    if (ROUTES.includes(r) && routeFromHash() !== r) {
      history.replaceState(null, '', `#${r}`);
    }
  }).observe(crumbEl, { childList: true, characterData: true, subtree: true });
}

window.addEventListener('hashchange', () => {
  const want = routeFromHash();
  if (!want) return;
  const btn = document.querySelector(`.rail button[data-route="${want}"]`);
  if (btn && !btn.classList.contains('active')) { pendingRoute = want; btn.click(); pendingRoute = null; }
});
