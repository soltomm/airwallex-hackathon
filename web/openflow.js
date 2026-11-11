/* OpenFlow presentation over Harbour's live state, and the New inquiry drawer.
   Every inquiry here goes to Harbour House itself: generated ones (an RFP plus a private brief for the buyer
   agent, see generator.py) or one you write. The seller agent, the buyer agent and the backend's rules do the
   rest; nothing in this file prices, approves or moves money. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const html = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = n => new Intl.NumberFormat('en-GB', {style:'currency', currency:'GBP', maximumFractionDigits:0}).format(n / 100);
  const exact = n => (n<0?'−':'')+new Intl.NumberFormat('en-GB', {style:'currency', currency:'GBP', minimumFractionDigits:Math.abs(n)%100?2:0}).format(Math.abs(n) / 100);
  const date = s => s ? new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB', {day:'numeric', month:'short'}) : 'To confirm';
  const longDate = s => new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB', {day:'numeric', month:'long', year:'numeric'});
  const time = s => new Date(s).toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  let view = 'overview', filter = 'all', search = '', trigger = null, catalog = null;
  function notify(message) { let el=$('wbToast'); if (!el) { el=document.createElement('div'); el.id='wbToast'; el.setAttribute('role','status'); } const dialog=document.querySelector('.wb-drawer[open]'); el.className=dialog?'wb-feedback':'toast'; if(dialog)dialog.insertBefore(el,dialog.children[1]);else document.body.append(el); el.textContent=message; setTimeout(()=>el.remove(),6000); }
  const actual = () => window.Harbour.state();
  const presetTitle = g => (catalog?.info?.[g.preset]?.title) || (g.preset ? g.preset.replace(/_/g, ' ') : 'Custom traits');
  // One status vocabulary: whose turn it is (Harbour.next). `turn` drives the chip colour, the filters and the
  // "Needs you" list; `stage` says how far the deal has got.
  const TURN = {you:'Needs you', bad:'Needs you', buyer:'With buyer', wait:'Seller agent', done:'Booked'};
  const stageOf = d => d.booking ? 'Booked' : ['expired','lost','declined'].includes(d.status) ? ({expired:'Lapsed',lost:'Closed',declined:'Declined'})[d.status]
    : d.accepted_version ? 'Agreed' : d.versions.some(v=>v.sent_at) ? `Offer v${d.versions.at(-1).n}` : d.versions.length ? `Drafting v${d.versions.at(-1).n}` : 'New inquiry';
  function rows() {
    return (actual()?.deals || []).map(d => {
      const v = d.versions.at(-1), r=d.requirements || {}, n=window.Harbour.next(d);
      const closed=['declined','expired','lost'].includes(d.status), turn=closed ? 'closed' : n.cls;
      const rooms = d.booking?.rooms || v?.terms.rooms;
      return {id:d.id, ref:d.ref, company:d.company, contact:d.sender, generated:d.generated, arrival:r.arrival, departure:r.departure, rooms:r.rooms || (rooms ? Math.max(...Object.values(rooms)) : 0),
        payer:d.payer_fx, turn, stage:stageOf(d), total:v?.pricing.total || 0, paid:d.finance_view?.net_received || 0, payout:d.payout_view, finance:d.finance_view,
        nights:rooms ? Object.values(rooms).reduce((a,b)=>a+b,0)+(v?.terms.vendor_lines||[]).filter(l=>l.category==='rooms').reduce((n,l)=>n+l.quantity,0) : (r.arrival && r.departure ? Math.max(0,(Date.parse(r.departure)-Date.parse(r.arrival))/864e5)*(r.rooms||0) : 0),
        next:closed ? stageOf(d) : n.short, ap:n.ap, review:['you','bad'].includes(n.cls), open:!closed && d.status!=='confirmed', booked:!!d.booking};
    });
  }
  const chip = d => `<span class="wb-turn ${d.turn}">${html(d.next)}</span>`;
  function metric(label, value, sub, extra='') { return `<div class="wb-kpi ${extra}"><dt>${label}</dt><dd>${value}</dd><p>${sub}</p></div>`; }
  // Where the seller agent is right now, for the top bar and the activity panel.
  function agentStatus(all) {
    const s=actual(), busy=s?.runner.busy, mode=s?.runner.settings?.seller_mode==='claude'?'Claude':'rules', you=all.filter(d=>d.review).length;
    if (busy && busy.startsWith('seller')) return {cls:'working', text:'Seller agent is working…', mode};
    if (you) return {cls:'waiting', text:`Seller agent · waiting on you (${you})`, mode};
    return {cls:'idle', text:'Seller agent · watching the inbox', mode};
  }
  function refresh() {
    const all=rows(), you=all.filter(d=>d.review), withBuyer=all.filter(d=>d.turn==='buyer'), open=all.filter(d=>d.open);
    const ag=agentStatus(all), s=actual(), awx=s?.provider==='airwallex';
    $('wbAgent').className=`wb-agent ${ag.cls}`; $('wbAgent').innerHTML=`<span class="wb-pulse"></span>${html(ag.text)}`; $('wbAgent').title=`Seller mode: ${ag.mode}`;
    if (view==='overview') {
      $('wbTitle').textContent = !s ? 'Group sales' : you.length ? `${you.length} ${you.length===1?'decision needs':'decisions need'} you` : 'Nothing needs you right now';
      $('wbTitle').classList.toggle('attention', you.length>0);
      $('wbLede').textContent = s ? [withBuyer.length && `${withBuyer.length} waiting on buyers`, open.length && `${money(open.reduce((n,d)=>n+d.total,0))} in open offers`,
        all.filter(d=>d.booked).length && `${all.filter(d=>d.booked).length} booked`].filter(Boolean).join(' · ') || 'No inquiries yet. Send one with New inquiry.' : 'Connecting to Harbour House…';
    }
    $('wbMetrics').innerHTML=metric('Needs you', you.length, you.length ? html(you.map(d=>d.next).slice(0,2).join(' · ')) : 'Nothing waiting on you', you.length ? 'attention' : '')
      +metric('Open pipeline',money(open.reduce((n,d)=>n+d.total,0)),`${open.length} active group ${open.length===1?'inquiry':'inquiries'}`)
      +metric('Group room nights',all.filter(d=>d.open||d.booked).reduce((n,d)=>n+d.nights,0),'Across active and booked groups')
      +metric('Deposits collected',money(all.reduce((n,d)=>n+d.paid,0)),`Net of refunds · ${awx?'Airwallex sandbox':'simulated Airwallex'}`);
    $('wbConnection').textContent=s ? `Harbour House connected · payments: ${awx?'Airwallex sandbox':'simulated Airwallex'} · seller agent: ${ag.mode}` : 'Connecting to Harbour House…';
    if (view==='payments') renderPayments(all); else renderPipeline(all);
  }
  const source = d => d.generated ? `${html(d.ref)} · <span class="wb-tag">Generated · ${html(presetTitle(d.generated))}</span>` : html(d.ref);
  const FILTERS=[['all','All'],['you','Needs you'],['wait','Seller agent'],['buyer','With buyer'],['done','Booked'],['closed','Closed']];
  const ORDER={you:0,bad:0,wait:1,buyer:2,done:3,closed:4};
  function table(all) {
    const filtered=all.filter(d=>(filter==='all'||filter===d.turn||filter==='you'&&d.turn==='bad') && `${d.company} ${d.ref} ${d.contact||''} ${d.payer?.origin?.country||''} ${d.payer?.origin?.local_currency||''}`.toLowerCase().includes(search.toLowerCase()))
      .sort((a,b)=>(ORDER[a.turn]??5)-(ORDER[b.turn]??5));
    const payer = d => d.payer?.origin ? window.FX?.summary(d.payer) : `<span class="muted">—</span>${d.payer?.latest ? `<span class="wb-next">${d.payer.latest.simulated?'Simulated FX':'FX'}: ${html(d.payer.latest.status)} ${html(d.payer.latest.currency)}</span>` : ''}`;
    return `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>Group</th><th>Payer origin</th><th>Stay dates</th><th>Rooms</th><th>Value</th><th>Next step</th></tr></thead><tbody>${filtered.map(d=>`<tr data-open="${html(d.id)}" class="${d.review?'needs-you':''}"><td><button class="wb-inquiry" data-open="${html(d.id)}">${html(d.company)}<span class="wb-source">${source(d)}</span></button></td><td>${payer(d)}</td><td>${date(d.arrival)} – ${date(d.departure)}</td><td>${d.rooms || '—'}</td><td>${d.total ? money(d.total) : '<span class="muted">Not quoted</span>'}</td><td>${chip(d)}${d.booked ? `<span class="wb-next">${exact(d.paid)} paid</span>` : d.stage!==d.next ? `<span class="wb-next">${html(d.stage)}</span>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="wb-empty">No inquiries match. Try another filter or send a new inquiry.</td></tr>'}</tbody></table></div>`;
  }
  // Decisions only the hotel can make, with the limit the seller hit. The drawer has the full card and the buttons.
  function attention(all) {
    const items=all.filter(d=>d.review);
    if (!items.length) return '';
    return `<section class="wb-attention" aria-label="Needs your decision"><div class="wb-attention-head">Needs your decision</div>${items.map(d=>{
      const rules=d.ap?.rules||[], why=rules.length ? rules.map(r=>`${html(r.rule)}: <b>${html(r.asked)}</b> <span class="muted">(seller alone ${html(r.alone)})</span>`).join(' · ') : d.ap ? html(d.ap.reasons?.[0]||'') : 'Handed to hotel staff. The seller stops until you act.';
      return `<div class="wb-attention-row"><div><b>${html(d.next)}</b> · ${html(d.company)} <span class="muted">${html(d.ref)}</span><div class="wb-attention-why">${why}</div></div><button class="wb-primary wb-review" data-open="${html(d.id)}">Review</button></div>`;}).join('')}</section>`;
  }
  // The seller agent's own actions, newest first, on the demo clock. Reads, dry-run pricing and notes stay in the
  // inquiry's AI Agent tab.
  const QUIET = new Set(['price_terms','log_reasoning']);
  function agentFeed(limit) {
    const s=actual(), H=window.Harbour, name=id=>s.deals.find(d=>d.id===id)?.company||'';
    const steps=(s?.agent_runs||[]).flatMap(r=>r.steps.filter(x=>!H.isRead(x.tool)&&!QUIET.has(x.tool)).map(x=>({...x,agent:r.agent}))).reverse().slice(0,limit);
    return steps.map(x=>`<div class="wb-feed-row"><span class="wb-feed-ico agent" aria-hidden="true">✦</span><div><b>${html(H.tool(x.tool))}</b>${x.ok?'':' <span class="wb-turn bad">refused</span>'}<span class="wb-feed-deal">${html(name(x.deal_id))}</span>
      <div class="wb-feed-detail">${html(H.tidy(x.summary).slice(0,160))}${H.tidy(x.summary).length>160?'…':''}</div></div><span class="wb-feed-at">${html(H.when(x.at))}</span></div>`).join('') || '<p class="wb-empty">The seller agent hasn’t acted yet.</p>';
  }
  // Payments, refunds and payouts as Airwallex reports them, plus the notifications it sent.
  const AWX = {deposit:'Deposit payment', refund:'Refund', payout:'Funds split to hotel', payout_reversal:'Funds split reversal'};
  function awxEvents(limit) {
    const s=actual(), H=window.Harbour, name=id=>s.deals.find(d=>d.id===id)?.company||'';
    const ops=(s?.operations||[]).map((o,i)=>({i, at:o.created_at, deal:o.deal_id, what:AWX[o.kind]||o.kind.replaceAll('_',' '), amount:(['refund','payout_reversal'].includes(o.kind)?-1:1)*o.amount,
      status:o.provider_status||o.status, ok:['succeeded','completed','paid','reversed'].includes(o.status), id:o.kind==='deposit'?o.payment_intent_id:o.provider_id}));
    const hooks=(s?.activity||[]).filter(a=>a.kind==='webhook').map((a,i)=>({i:1e4+i, at:a.at, deal:a.deal_id, what:'Notification received', detail:H.tidy(a.text).replace(/^Provider event /,'')}));
    const all=[...ops,...hooks].sort((a,b)=>b.at.localeCompare(a.at)||b.i-a.i).slice(0,limit);
    return all.map(e=>`<div class="wb-feed-row"><span class="wb-feed-ico awx" aria-hidden="true">⇄</span><div><b>${html(e.what)}</b>${e.amount!=null?` <span class="wb-amount">${exact(e.amount)}</span>`:''}<span class="wb-feed-deal">${html(name(e.deal))}</span>
      <div class="wb-feed-detail">${e.status?`<span class="wb-status-code ${e.ok?'ok':''}">${html(e.status)}</span>`:''}${e.id?`<code>${html(e.id)}</code>`:''}${e.detail?html(e.detail.slice(0,140)):''}</div></div><span class="wb-feed-at">${html(H.when(e.at))}</span></div>`).join('')
      || '<p class="wb-empty">No payments yet. The first one appears when a buyer pays a deposit.</p>';
  }
  function awxTotals(all) {
    const sum=f=>all.reduce((n,d)=>n+(f(d)||0),0), po=d=>d.payout && d.payout.status!=='not_due' ? d.payout : null;
    const t={collected:sum(d=>d.finance?.received), refunded:sum(d=>d.finance?.refunded), hotel:sum(d=>po(d)?.hotel_net), fee:sum(d=>po(d)?.platform_fee)};
    return `<div class="wb-awx-totals"><div><span>Collected</span><b>${exact(t.collected)}</b></div><div><span>Refunded</span><b>${exact(t.refunded)}</b></div><div><span>Paid to hotel</span><b>${exact(t.hotel)}</b></div><div><span>Platform fee</span><b>${exact(t.fee)}</b></div></div>`;
  }
  function renderPipeline(all) {
    const ag=agentStatus(all), s=actual();
    $('wbBody').innerHTML=`${attention(all)}<div class="wb-columns"><section class="wb-panel"><div class="wb-panel-head"><h2>Group inquiries <span class="muted small">${all.length}</span></h2><span class="muted small">Sorted by whose turn it is</span></div>
      <div class="wb-toolbar"><div class="wb-filters" aria-label="Filter inquiries">${FILTERS.map(([key,label])=>{const c=key==='all'?all.length:all.filter(d=>d.turn===key||key==='you'&&d.turn==='bad').length;return `<button data-filter="${key}" aria-pressed="${filter===key}">${label}<span class="wb-count">${c}</span></button>`;}).join('')}</div><label class="wb-search"><span class="small muted" hidden>Search inquiries</span><input id="wbSearch" type="search" aria-label="Search inquiries" placeholder="⌕  Search inquiries" value="${html(search)}"></label></div><div id="wbTable">${table(all)}</div></section>
      <section class="wb-panel"><div class="wb-panel-head"><h2>Your next moves</h2></div><div class="wb-moves"></div></section></div>
      <div class="wb-bottom-grid"><section class="wb-panel"><div class="wb-panel-head"><h2>Seller agent</h2><span class="wb-agent ${ag.cls}"><span class="wb-pulse"></span>${html(ag.cls==='working'?'Working…':ag.cls==='waiting'?'Waiting on you':'Watching the inbox')}</span></div><div class="wb-feed">${agentFeed(6)}</div></section>
      <section class="wb-panel wb-awx"><div class="wb-panel-head"><h2><span class="wb-awx-badge">Airwallex</span> Payments</h2><button class="wb-link" data-wb-view="payments">All payments →</button></div>${awxTotals(all)}<div class="wb-feed">${awxEvents(5)}</div></section></div>`;
    // Update only the table while typing so keyboard focus and selection stay intact.
    if(window.Vendors)document.querySelector('.wb-moves').innerHTML=window.Vendors.nextMoves(all);
    $('wbSearch').oninput=e=>{search=e.target.value; $('wbTable').innerHTML=table(rows());};
  }
  function renderPayments(all) {
    const src = actual()?.provider==='airwallex' ? 'Airwallex sandbox' : 'Simulated Airwallex';
    $('wbBody').innerHTML=`<section class="wb-panel wb-awx"><div class="wb-panel-head"><div><h2><span class="wb-awx-badge">Airwallex</span> Group deposits</h2><p class="wb-sub">The platform collects each deposit for Harbour House's Airwallex connected account, then pays the hotel its share by funds split. Refunds take the hotel's share back the same way.</p></div></div>${awxTotals(all)}<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>Group</th><th>Deposit received (net)</th><th>Paid to hotel (net)</th><th>Platform fee</th><th>Source</th><th>Status</th></tr></thead><tbody>${all.map(d=>{const po=d.payout, paidOut=po && po.status!=='not_due';return `<tr data-open="${html(d.id)}"><td><button class="wb-inquiry" data-open="${html(d.id)}">${html(d.company)}</button></td><td>${exact(d.paid)}</td><td>${paidOut?exact(po.hotel_net):'—'}</td><td>${paidOut?exact(po.platform_fee):'—'}</td><td>${src}</td><td>${d.paid>0?(paidOut?({paid:'Paid to hotel',failed:'Payout refused',unknown:'Payout unknown',no_account:'No hotel account',pending:'Payout pending'})[po.status]||'Received':'Received'): 'Not received'}</td></tr>`}).join('') || '<tr><td colspan="6" class="wb-empty">No group deposits yet.</td></tr>'}</tbody></table></div></section>
      <section class="wb-panel" style="margin-top:22px"><div class="wb-panel-head"><h2>Airwallex activity</h2><span class="muted small">Every payment, refund, payout and notification</span></div><div class="wb-feed">${awxEvents(50)}</div></section>`;
  }
  function navigate(next) {
    view=next; location.hash=''; $('workbench').hidden=false;
    $('wbBreadcrumb').textContent=next==='overview'?'Overview':'Payments';
    if(next==='payments'){$('wbTitle').textContent='Every deposit, accounted for.';$('wbTitle').classList.remove('attention');$('wbLede').textContent='Payments, refunds and hotel payouts through Airwallex.';}
    document.querySelectorAll('[data-wb-view]').forEach(b=>{if(b.dataset.wbView===next)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    refresh();
  }
  function showDrawer(title, content, label='NEW INQUIRY') {
    const dialog=$('wbDrawer');
    $('wbToast')?.remove();
    if (!dialog.open) trigger=document.activeElement;
    $('wbDrawerLabel').textContent=label;
    $('wbDrawerBody').innerHTML=`<h2 id="wbDrawerTitle" tabindex="-1">${title}</h2>${content}`;
    if(!dialog.open) dialog.showModal();
    $('wbDrawerTitle').focus();
  }

  // ---- New inquiry: generated (RFP + private buyer brief) or written by hand ----------------------------
  const COUNTRIES = [['', 'United Kingdom (GBP)'], ['DE', 'Germany (EUR)'], ['FR', 'France (EUR)'], ['NL', 'Netherlands (EUR)'],
    ['US', 'United States (USD)'], ['AU', 'Australia (AUD)'], ['SG', 'Singapore (SGD)'], ['JP', 'Japan (JPY)']];
  const TRAIT_LABEL = {fits:'Rooms free on the dates', short_rooms:'Fewer rooms free than asked', too_soon:'Too soon to sell to a group',
    comfortable:'Comfortable budget', tight:'Tight: at the seller’s floor', below_floor:'Below the seller’s floor'};
  const opts = (name, values, pick) => `<select id="wbT_${name}">${values.map(([v, l]) => `<option value="${html(v)}" ${String(v)===String(pick ?? '')?'selected':''}>${html(l)}</option>`).join('')}</select>`;
  function traitForm(c) {
    const d = c.defaults, t = c.traits, pairs = k => t[k].map(v => [v, TRAIT_LABEL[v] || v]);
    return `<details class="wb-traits"><summary>Or choose the traits yourself</summary>
      <p class="wb-sub">Dates, rooms and budget are worked out from the live calendar and the hotel’s own rate bands.</p>
      <div class="wb-terms">
        <label class="wb-field">Dates${opts('availability', pairs('availability'), d.availability)}</label>
        <label class="wb-field">Price pressure${opts('price', pairs('price'), d.price)}</label>
        <label class="wb-field">Attrition the buyer wants${opts('attrition', [['', 'Hotel standard'], ['20', '20% (traded for a bigger deposit)']], d.attrition)}</label>
        <label class="wb-field">Room type${opts('room_type', [['', 'Either'], ['twin', 'Twin'], ['king', 'King']], d.room_type)}</label>
        <label class="wb-field">Payer${opts('payer_country', COUNTRIES, d.payer_country)}</label>
        <label class="wb-field">Later change${opts('change_rooms', [['0', 'None'], ['1', '1 room fewer a night, then a refund'], ['2', '2 rooms fewer a night, then a refund']], d.change_rooms)}</label>
        <label class="wb-field">Meeting room${opts('meeting', [['', 'Not needed'], ['1', 'Thames Room, one day']], d.meeting ? '1' : '')}</label>
      </div>
      <fieldset class="wb-missing"><legend>Leave out of the RFP (the seller has to ask)</legend>
        ${t.missing.map(m => `<label><input type="checkbox" name="wbMissing" value="${html(m)}"> ${html({payer:'Who pays', taxes:'Whether the budget includes taxes', layout:'Meeting layout'}[m] || m)}</label>`).join('')}</fieldset>
      <button class="wb-primary" type="button" id="wbCustom">Send this inquiry to Harbour House</button></details>`;
  }
  function readTraits() {
    const v = k => $('wbT_' + k).value, out = {availability: v('availability'), price: v('price'),
      change_rooms: Number(v('change_rooms')), meeting: v('meeting') === '1',
      missing: [...document.querySelectorAll('[name=wbMissing]:checked')].map(x => x.value)};
    if (v('attrition')) out.attrition = Number(v('attrition'));
    if (v('room_type')) out.room_type = v('room_type');
    if (v('payer_country')) out.payer_country = v('payer_country');
    return out;
  }
  function buyerToggle(mode) {
    return `<div class="wb-buyer-mode" role="group" aria-label="Who answers for the buyer">
      <span>Buyer replies</span>
      <button type="button" data-buyer-mode="agent" aria-pressed="${mode==='agent'}">Automatically (buyer agent)</button>
      <button type="button" data-buyer-mode="presenter" aria-pressed="${mode!=='agent'}">By you, in the buyer’s mailbox</button></div>
      <p class="wb-sub">${mode==='agent' ? 'The buyer agent answers for every buyer with a brief: generated inquiries and the two scenario ones. Approvals and paying the deposit stay with you.' : 'You send each buyer’s replies from its mailbox, with a draft from its brief ready to send.'}</p>`;
  }
  async function simulator() {
    showDrawer('Let the inquiry come to you.', '<p class="wb-lede">Loading inquiry types…</p>');
    try { catalog = await window.Harbour.presets(); }
    catch (err) { $('wbDrawerBody').innerHTML='<h2 id="wbDrawerTitle" tabindex="-1">Let the inquiry come to you.</h2><p class="wb-lede">Couldn’t load the inquiry types. Write your own below instead.</p>' + ownForm(); wireOwn(); return; }
    const S = actual(), b = S?.bookable;
    const cards = Object.keys(catalog.presets).map(k => { const i = catalog.info[k] || {title: k, shows: ''};
      return `<button class="wb-channel" data-preset="${html(k)}"><b>${html(i.title)}</b><p>${html(i.shows)}</p><span class="wb-channel-action">Send this inquiry →</span></button>`; }).join('');
    showDrawer('Let the inquiry come to you.', `<p class="wb-lede">Send Harbour House a generated group inquiry. Each one comes with a private brief for the buyer agent: its answers and goals, which the seller never sees. From the email on, the seller agent, the buyer agent and the backend’s rules take over${S?.provider==='airwallex' ? ', with real Airwallex sandbox payments' : ''}.</p>
      ${buyerToggle(catalog.buyer_mode)}
      <label class="wb-field wb-seed">Seed (optional)<input id="wbSeed" inputmode="numeric" pattern="[0-9]*" placeholder="Leave blank for a new inquiry each time"></label>
      <p class="wb-sub">The same seed sends the same inquiry against the same calendar. After a reset, reuse a seed to replay a run.</p>
      <div class="wb-channel-grid">${cards}</div>
      ${traitForm(catalog)}
      ${ownForm(b)}`, 'NEW INQUIRY');
    wireOwn();
    $('wbCustom').onclick = () => generate({traits: readTraits()});
  }
  async function generate(body) {
    const seed = $('wbSeed')?.value.trim();
    if (seed && !/^\d+$/.test(seed)) { notify('The seed is a whole number, or leave it blank.'); return; }
    if (seed) body.seed = Number(seed);
    document.querySelectorAll('[data-preset],#wbCustom').forEach(x => x.disabled = true);
    try {
      const r = await window.Harbour.generate(body);
      $('wbDrawer').close(); refresh(); window.Harbour.open(r.deal_id);
      notify(`Sent: ${presetTitle(r)} · seed ${r.seed}. Reuse the seed after a reset to replay it.`);
    } catch (err) {
      notify(err.message);
      document.querySelectorAll('[data-preset],#wbCustom').forEach(x => x.disabled = false);
    }
  }
  function ownForm(b) {
    const scope = b ? `for nights from ${longDate(b.first_night)} to ${longDate(b.last_night)}, with at least ${b.min_days_before_arrival} days’ notice` : 'for its bookable nights';
    return `<form id="wbInbound"><h3>Or write your own</h3>
        <p class="wb-sub">A real, unscripted RFP. It has no brief, so nobody answers for the buyer automatically: reply from its mailbox.</p>
        <label class="wb-field">From<input id="wbInquiryFrom" required maxlength="200" value="Guest <guest@example.com>"></label>
        <label class="wb-field">Subject<input id="wbInquirySubject" required maxlength="200" value="Group inquiry"></label>
        <label class="wb-field">Inquiry<textarea id="wbInquiryText" rows="8" required maxlength="5000" placeholder="e.g. Please quote 6 king rooms for 12 colleagues arriving 10 May and departing 13 May 2027, with breakfast. Our budget is £4,500 inclusive of taxes. Acme Demo Ltd pays."></textarea></label>
        <p class="wb-sub">Harbour House sells twin and king rooms, breakfast and the Thames Room (classroom layout) ${html(scope)}. Anything else is handed to hotel staff. Mention who pays and whether the budget includes taxes, or the agent will ask.</p>
        <button class="wb-primary" type="submit" id="wbSend">Send inquiry to Harbour House</button></form>`;
  }
  function wireOwn() {
    $('wbInbound').onsubmit=async e=>{
      e.preventDefault();
      const from=$('wbInquiryFrom').value.trim(), subject=$('wbInquirySubject').value.trim(), body=$('wbInquiryText').value.trim();
      if(!from||!body){notify('Add who it is from and the inquiry text.');return;}
      $('wbSend').disabled=true;
      try {
        const r=await window.Harbour.inbound(from,subject||'Group inquiry',body);
        if(r.duplicate){
          const msg=`Harbour House already received this exact email from ${from} (${r.ref||'an earlier inquiry'}), so it wasn't sent again. To start a new inquiry, change the text or the sender.`;
          $('wbSend').disabled=false; notify(msg);
          setTimeout(()=>{ $('wbDrawer').close(); refresh(); window.Harbour.open(r.deal_id); notify(msg); }, 2500); return;
        }
        $('wbDrawer').close(); refresh(); window.Harbour.open(r.deal_id);
      }
      catch(err){ notify(err.message); $('wbSend').disabled=false; }
    };
  }
  async function setBuyerMode(mode) {
    try { await window.Harbour.setBuyerMode(mode); catalog.buyer_mode = mode;
      const box = document.querySelector('.wb-buyer-mode'); box.nextElementSibling.remove(); box.outerHTML = buyerToggle(mode);
      notify(mode==='agent' ? 'The buyer agent now answers for buyers with a brief.' : 'You answer for the buyers now.'); }
    catch (err) { notify(err.message); }
  }

  function open(id) {
    if(!actual()?.deals.some(d=>d.id===id)){notify('This inquiry is no longer available.');return;}
    trigger=document.activeElement; window.Harbour.open(id);
  }
  document.addEventListener('click',e=>{
    const button=e.target.closest('button,a,tr[data-open]');if(!button)return;
    if(button.dataset.wbView)navigate(button.dataset.wbView);
    if(button.hasAttribute('data-wb-close'))button.closest('dialog').close();
    if(button.dataset.filter){filter=button.dataset.filter;refresh();document.querySelector(`[data-filter="${filter}"]`).focus();}
    if(button.dataset.open)open(button.dataset.open);
    if(button.dataset.preset)generate({preset: button.dataset.preset});
    if(button.dataset.buyerMode && button.getAttribute('aria-pressed')!=='true')setBuyerMode(button.dataset.buyerMode);
  });
  for(const dialog of [$('wbDrawer'),$('liveDrawer')]) {
    dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right)dialog.close();}});
    dialog.addEventListener('close',()=>{if(trigger?.isConnected)trigger.focus();else $('wbSimulate').focus();});
  }
  $('wbSimulate').onclick=simulator;
  $('wbAgent').onclick=()=>{if(view!=='overview'||location.hash)navigate('overview');setTimeout(()=>document.querySelector('.wb-bottom-grid')?.scrollIntoView({behavior:'smooth',block:'start'}),50);};
  $('wbAvailability').onclick=()=>{location.hash='availability';}; $('wbRules').onclick=()=>{location.hash='rules';};
  // Preserve focus while Harbour polls, including keyboard search and draft entry.
  window.OpenFlow={refresh:()=>{if(document.activeElement?.closest('#workbench'))return;refresh();}};
  const mobile=matchMedia('(max-width:760px)');
  const propertyTools=()=>{document.querySelector('.wb-bottom').open=!mobile.matches;};
  propertyTools(); mobile.addEventListener('change',propertyTools);
  refresh();
  window.Harbour.presets?.().then(c=>{catalog=c;refresh();}).catch(()=>{});
})();
