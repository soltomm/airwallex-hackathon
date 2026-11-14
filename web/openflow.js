/* OpenFlow presentation over Groupy's live state, and the New inquiry drawer.
   Every inquiry here goes to Groupy itself: generated ones (an RFP plus a private brief that drafts the
   buyer's replies, see generator.py) or one you write. You reply as the buyer from its mailbox; the seller agent
   and the backend's rules do the rest; nothing in this file prices, approves or moves money. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const html = s => String(s ?? '').replace(/\bHarbour\s+House\b/gi, 'Groupy').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = n => new Intl.NumberFormat('en-GB', {style:'currency', currency:'GBP', maximumFractionDigits:0}).format(n / 100);
  const date = s => s ? new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB', {day:'numeric', month:'short'}) : 'To confirm';
  const longDate = s => new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB', {day:'numeric', month:'long', year:'numeric'});
  const time = s => new Date(s).toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  let view = 'overview', filter = 'all', search = '', trigger = null, catalog = null;
  const labels = {draft:'New inquiry', approval:'Internal approval', proposal:'Proposal', confirmed:'Confirmed'};
  const NEW = ['draft', 'unavailable'];
  function notify(message) { let el=$('wbToast'); if (!el) { el=document.createElement('div'); el.id='wbToast'; el.setAttribute('role','status'); } const dialog=document.querySelector('.wb-drawer[open]'); el.className=dialog?'wb-feedback':'toast'; if(dialog)dialog.insertBefore(el,dialog.children[1]);else document.body.append(el); el.textContent=message; setTimeout(()=>el.remove(),6000); }
  const actual = () => window.Harbour.state();
  const presetTitle = g => (catalog?.info?.[g.preset]?.title) || (g.preset ? g.preset.replace(/_/g, ' ') : 'Custom traits');
  function rows() {
    return (actual()?.deals || []).map(d => {
      const v = d.versions.at(-1), r=d.requirements || {}, n=window.Harbour.next(d);
      const status=['confirmed','declined','expired'].includes(d.status) ? d.status : n.cls==='you' ? 'approval' : ['new','intake','needs_info'].includes(d.status) ? 'draft' : d.status==='handoff' ? 'unavailable' : 'proposal';
      const rooms = d.booking?.rooms || v?.terms.rooms;
      return {id:d.id, company:d.company, contact:d.sender, generated:d.generated, arrival:r.arrival, departure:r.departure, rooms:(rooms ? Math.max(...Object.values(rooms)) : 0) || r.rooms || 0,
        payer:d.payer_fx, status, label:d.pricing_review?.status==='pending'?'Awaiting revenue review':({handoff:'Staff attention',declined:'Declined',expired:'Expired'})[d.status] || labels[status], total:v?.pricing.total || 0, paid:d.finance_view?.net_received || 0, payout:d.payout_view,
        nights:rooms ? Object.values(rooms).reduce((a,b)=>a+b,0)+(v?.terms.vendor_lines||[]).filter(l=>l.category==='rooms').reduce((n,l)=>n+l.quantity,0) : (r.arrival && r.departure ? Math.max(0,(Date.parse(r.departure)-Date.parse(r.arrival))/864e5)*(r.rooms||0) : 0),
        next:n.short, review:['you','bad'].includes(n.cls) || ['new','intake'].includes(d.status), open:!['confirmed','declined','expired'].includes(d.status)};
    });
  }
  function metric(label, value, sub, extra='') { return `<div class="wb-kpi ${extra}"><dt>${label}</dt><dd>${value}</dd><p>${sub}</p></div>`; }
  function refresh() {
    const all=rows(), pending=all.filter(d=>d.review||(window.Vendors?.pendingIds()||[]).includes(d.id)).length, fresh=all.filter(d=>NEW.includes(d.status)).length;
    $('wbMetrics').innerHTML=metric('Open pipeline',money(all.filter(d=>d.open).reduce((n,d)=>n+d.total,0)),`${all.filter(d=>d.open).length} active group inquiries`)
      +metric('Group room nights',all.filter(d=>d.open||d.status==='confirmed').reduce((n,d)=>n+d.nights,0),'Across active and confirmed groups')
      +metric(`New inquiries ${pending ? `<span class="wb-badge">${pending} to review</span>` : ''}`,fresh,'A new opportunity to welcome a group','highlight')
      +metric('Deposits collected',money(all.reduce((n,d)=>n+d.paid,0)),'Net of refunds');
    const s=actual(); $('wbConnection').textContent=s ? `Groupy connected · ${s.provider==='airwallex'?'Airwallex sandbox':'Simulated Groupy payments'} · ${s.runner.busy || 'Seller watching inbox'}` : 'Connecting to Groupy…';
    if (view==='payments') renderPayments(all); else renderPipeline(all);
  }
  const source = d => d.generated ? `Generated · ${presetTitle(d.generated)}` : 'Groupy · Connected';
  function table(all) {
    const filtered=all.filter(d=>(filter==='all'||filter==='new'&&NEW.includes(d.status)||filter===d.status) && `${d.company} ${d.contact||''} ${d.payer?.origin?.country||''} ${d.payer?.origin?.local_currency||''}`.toLowerCase().includes(search.toLowerCase()));
    return `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>Group / organizer</th>${window.FX?'<th>Payer origin</th>':''}<th>Stay dates</th><th>Rooms</th><th>Proposal value</th><th>Status</th></tr></thead><tbody>${filtered.map(d=>`<tr data-open="${html(d.id)}"><td><button class="wb-inquiry" data-open="${html(d.id)}">${html(d.company)}<span class="wb-source">${html(source(d))}</span></button></td>${window.FX?`<td>${window.FX.summary(d.payer)}</td>`:''}<td>${date(d.arrival)} – ${date(d.departure)}</td><td>${d.rooms || '—'}</td><td>${d.total ? money(d.total) : 'Not quoted'}</td><td><span class="wb-status ${d.status}">${html(d.label)}</span><span class="wb-next">${html(d.next)}</span></td></tr>`).join('') || '<tr><td colspan="${window.FX?6:5}" class="wb-empty">No inquiries match. Try another filter or send a new inquiry.</td></tr>'}</tbody></table></div>`;
  }
  function renderPipeline(all) {
    const moves=all.filter(d=>d.review).slice(0,6);
    const activity=(actual()?.activity || []).map(a=>({at:a.real_at||a.at,text:a.text||a.kind,company:'Groupy',id:a.deal_id})).sort((a,b)=>b.at.localeCompare(a.at)).slice(0,5);
    const stages=[['New inquiries', NEW],['Internal approval',['approval']],['Proposal',['proposal']],['Confirmed',['confirmed']]];
    $('wbBody').innerHTML=`<div class="wb-columns"><section class="wb-panel"><div class="wb-panel-head"><h2>Group inquiries <span class="muted small">${all.length}</span></h2><span class="muted small">${all.filter(d=>d.review||(window.Vendors?.pendingIds()||[]).includes(d.id)).length} pending action</span></div>
      <div class="wb-toolbar"><div class="wb-filters" aria-label="Pipeline filters">${[['new','New'],['all','All inquiries'],['approval','Internal approval'],['proposal','Proposal'],['confirmed','Confirmed']].map(([key,label])=>`<button data-filter="${key}" aria-pressed="${filter===key}">${label}</button>`).join('')}</div><label class="wb-search"><span class="small muted" hidden>Search inquiries</span><input id="wbSearch" type="search" aria-label="Search inquiries" placeholder="⌕  Search inquiries" value="${html(search)}"></label></div><div id="wbTable">${table(all)}</div></section>
      <section class="wb-panel"><div class="wb-panel-head"><h2>Your next moves</h2><span class="muted small">↗</span></div><div class="wb-moves">${moves.map(d=>`<button class="wb-move" data-open="${html(d.id)}"><span class="wb-move-icon" aria-hidden="true">${d.status==='approval'?'✓':'↗'}</span><span><b>${html(d.next)}</b><small>${html(d.company)}</small></span></button>`).join('') || '<p class="wb-empty">You’re all caught up.<br>We’ll keep an eye on the inbox.</p>'}</div></section></div>
      <div class="wb-bottom-grid"><section class="wb-panel"><div class="wb-panel-head"><h2>Pipeline at a glance</h2><span class="muted small">Current inquiries</span></div><div class="wb-summary">${stages.map(([label,statuses])=>{const count=all.filter(d=>statuses.includes(d.status)).length;return `<div class="wb-summary-row"><span>${label}</span><b>${count}</b></div><div class="wb-track"><span style="width:${all.length?count/all.length*100:0}%"></span></div>`;}).join('')}</div></section>
      <section class="wb-panel"><div class="wb-panel-head"><h2>Recent activity</h2><span class="muted small">Latest updates</span></div><div class="wb-summary">${activity.map(a=>`<div class="wb-summary-row"><span>${html(a.text)}<span class="wb-source">${html(a.company)}</span></span><span class="muted">${time(a.at)}</span></div>`).join('') || '<p class="wb-empty">New activity will appear here.</p>'}</div></section></div>`;
    // Update only the table while typing so keyboard focus and selection stay intact.
    if(window.Vendors)document.querySelector('.wb-moves').innerHTML=window.Vendors.nextMoves(all);
    $('wbSearch').oninput=e=>{search=e.target.value; $('wbTable').innerHTML=table(rows());};
  }
  function renderPayments(all) {
    const src = actual()?.provider==='airwallex' ? 'Airwallex sandbox' : 'Groupy simulated provider';
    $('wbBody').innerHTML=`<section class="wb-panel"><div class="wb-panel-head"><div><h2>Group deposits</h2><p class="wb-sub">Deposits are collected by the platform for Groupy's Airwallex connected account; its share is paid out by funds split.</p></div></div><div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>Group</th><th>Deposit received (net)</th><th>Paid to hotel (net)</th><th>Platform fee</th><th>Source</th><th>Status</th></tr></thead><tbody>${all.map(d=>{const po=d.payout, paidOut=po && po.status!=='not_due';return `<tr data-open="${html(d.id)}"><td><button class="wb-inquiry" data-open="${html(d.id)}">${html(d.company)}</button></td><td>${money(d.paid)}</td><td>${paidOut?money(po.hotel_net):'—'}</td><td>${paidOut?money(po.platform_fee):'—'}</td><td>${src}</td><td>${d.paid>0?(paidOut?({paid:'Paid to hotel',failed:'Payout refused',unknown:'Payout unknown',no_account:'No hotel account',pending:'Payout pending'})[po.status]||'Received':'Received'): 'Not received'}</td></tr>`}).join('') || '<tr><td colspan="6" class="wb-empty">No group deposits yet.</td></tr>'}</tbody></table></div></section>`;
  }
  function navigate(next) {
    view=next; location.hash=''; $('workbench').hidden=false;
    $('wbBreadcrumb').textContent=next==='overview'?'Overview':'Payments';
    $('wbTitle').textContent=next==='overview'?'A little more room to grow.':'Every deposit, accounted for.';
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

  // ---- New inquiry: written by hand (the main path), or a generated test inquiry with a private buyer brief ----
  async function simulator() {
    showDrawer('Let the inquiry come to you.', '<p class="wb-lede">Loading inquiry types…</p>');
    try { catalog = await window.Harbour.presets(); }
    catch (err) { $('wbDrawerBody').innerHTML='<h2 id="wbDrawerTitle" tabindex="-1">Let the inquiry come to you.</h2><p class="wb-lede">Write a group inquiry the way a buyer would. (Generated test inquiries couldn’t be loaded.)</p>' + thirdPartyForm() + ownForm(); wireOwn(); return; }
    const S = actual(), b = S?.bookable;
    const cards = Object.keys(catalog.presets).map(k => { const i = catalog.info[k] || {title: k, shows: ''};
      return `<button class="wb-channel" data-preset="${html(k)}"><b>${html(i.title)}</b><p>${html(i.shows)}</p><span class="wb-channel-action">Send this inquiry →</span></button>`; }).join('');
    showDrawer('Let the inquiry come to you.', `<p class="wb-lede">Write a group inquiry the way a buyer would. The seller agent reads it like any other email, and the backend’s rules decide what it can offer${S?.provider==='airwallex' ? ', with real Airwallex sandbox payments' : ''}.</p>
      ${thirdPartyForm()}
      ${ownForm(b)}
      <details class="wb-traits wb-generated"><summary>Generated test inquiries</summary>
        <p class="wb-sub">A new inquiry each time you click. Each one comes with a private brief that drafts the buyer’s replies in its mailbox; the seller only sees the email, never the brief or which test it is.</p>
        <div class="wb-channel-grid">${cards}</div>
      </details>`, 'NEW INQUIRY');
    wireOwn();
  }
  async function generate(body) {
    body.third_party = thirdPartyOptions();
    document.querySelectorAll('[data-preset]').forEach(x => x.disabled = true);
    try {
      const r = await window.Harbour.generate(body);
      $('wbDrawer').close(); refresh(); window.Harbour.open(r.deal_id);
      notify(`Sent: ${presetTitle(r)}.`);
    } catch (err) {
      notify(err.message);
      document.querySelectorAll('[data-preset]').forEach(x => x.disabled = false);
    }
  }
  function thirdPartyForm(){if(!window.Vendors)return '';return `<fieldset class="card"><legend>Third-party simulation</legend><label><input id="wbThirdParty" type="checkbox"> Needs third-party services</label><label class="wb-field">Service<select id="wbThirdPartyCategory">${['av','catering','meeting','rooms','technician','amenities'].map(c=>`<option value="${c}">${c}</option>`).join('')}</select></label><label><input id="wbThirdPartyQuote" type="checkbox" checked> Include a hypothetical quote response</label><p class="small muted">Creates a simulated preferred-provider request once stay dates are recorded. Review the quote, approve it and simulate confirmation in the inquiry’s third-party workspace. No provider is contacted.</p></fieldset>`;}
  function thirdPartyOptions(){return $('wbThirdParty')?.checked?{category:$('wbThirdPartyCategory').value,quote_response:$('wbThirdPartyQuote').checked}:null;}
  function ownForm(b) {
    const scope = b ? `for nights from ${longDate(b.first_night)} to ${longDate(b.last_night)}, with at least ${b.min_days_before_arrival} days’ notice` : 'for its bookable nights';
    return `<form id="wbInbound"><h3>Write your own</h3>
        <p class="wb-sub">A real, unscripted RFP. You reply as the buyer from its mailbox.</p>
        <label class="wb-field">From<input id="wbInquiryFrom" required maxlength="200" value="Guest <guest@example.com>"></label>
        <label class="wb-field">Subject<input id="wbInquirySubject" required maxlength="200" value="Group inquiry"></label>
        <label class="wb-field">Inquiry<textarea id="wbInquiryText" rows="8" required maxlength="5000" placeholder="e.g. Please quote 6 king rooms for 12 colleagues arriving 10 May and departing 13 May 2027, with breakfast. Our budget is £4,500 inclusive of taxes. Acme Demo Ltd pays."></textarea></label>
        <p class="wb-sub">Groupy sells twin and king rooms, breakfast and the Thames Room (classroom layout) ${html(scope)}. Anything else is handed to hotel staff. Mention who pays and whether the budget includes taxes, or the agent will ask.</p>
        <button class="wb-primary" type="submit" id="wbSend">Send inquiry to Groupy</button></form>`;
  }
  function wireOwn() {
    $('wbInbound').onsubmit=async e=>{
      e.preventDefault();
      const from=$('wbInquiryFrom').value.trim(), subject=$('wbInquirySubject').value.trim(), body=$('wbInquiryText').value.trim();
      if(!from||!body){notify('Add who it is from and the inquiry text.');return;}
      $('wbSend').disabled=true;
      try {
        const r=await window.Harbour.inbound(from,subject||'Group inquiry',body,thirdPartyOptions());
        if(r.duplicate){
          const msg=`Groupy already received this exact email from ${from} (${r.ref||'an earlier inquiry'}), so it wasn't sent again. To start a new inquiry, change the text or the sender.`;
          $('wbSend').disabled=false; notify(msg);
          setTimeout(()=>{ $('wbDrawer').close(); refresh(); window.Harbour.open(r.deal_id); notify(msg); }, 2500); return;
        }
        $('wbDrawer').close(); refresh(); window.Harbour.open(r.deal_id);
      }
      catch(err){ notify(err.message); $('wbSend').disabled=false; }
    };
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
  });
  for(const dialog of [$('wbDrawer'),$('liveDrawer')]) {
    dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right)dialog.close();}});
    dialog.addEventListener('close',()=>{if(trigger?.isConnected)trigger.focus();else $('wbSimulate').focus();});
  }
  $('wbSimulate').onclick=simulator;
  $('wbAvailability').onclick=()=>{location.hash='availability';}; $('wbRules').onclick=()=>{location.hash='rules';};
  // Preserve focus while Harbour polls, including keyboard search and draft entry.
  window.OpenFlow={refresh:()=>{if(document.activeElement?.closest('#workbench'))return;refresh();}};
  const mobile=matchMedia('(max-width:760px)');
  const propertyTools=()=>{document.querySelector('.wb-bottom').open=!mobile.matches;};
  propertyTools(); mobile.addEventListener('change',propertyTools);
  refresh();
  window.Harbour.presets?.().then(c=>{catalog=c;refresh();}).catch(()=>{});
})();
