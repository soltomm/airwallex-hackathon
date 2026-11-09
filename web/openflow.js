/* OpenFlow presentation over Harbour's existing state, plus an isolated simulator. */
(() => {
  'use strict';
  const M = window.OpenFlowDemo, $ = id => document.getElementById(id);
  const html = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = n => new Intl.NumberFormat('en-GB', {style:'currency', currency:'GBP', maximumFractionDigits:0}).format(n / 100);
  const date = s => s ? new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB', {day:'numeric', month:'short'}) : 'To confirm';
  const time = s => new Date(s).toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  const key = 'openflow-harbour-demo-v1';
  let demo = [];
  try { const saved = JSON.parse(localStorage.getItem(key) || '[]'); if (Array.isArray(saved) && saved.every(d => d.demo === true && d.id?.startsWith('demo-') && Array.isArray(d.timeline) && M.pricing(d))) demo = saved; } catch {}
  let view = 'overview', filter = 'all', search = '', active = null, trigger = null;
  const labels = {draft:'New inquiry', unavailable:'Dates unavailable', alternative_draft:'Review alternative', alternative_sent:'Awaiting reply', approval:'Internal approval', proposal:'Proposal', confirmed:'Confirmed'};
  const action = d => ({draft:'Prepare proposal', unavailable:'Review alternative dates', alternative_draft:'Review alternative reply', alternative_sent:'Waiting on customer', approval:'Review manager approval', proposal:'Awaiting customer acceptance', confirmed:'View operations handoff'})[d.status] || '';
  function save() { try { localStorage.setItem(key, JSON.stringify(demo)); } catch { notify('Browser storage is unavailable. Simulator changes will last only in this page.'); } }
  function notify(message) { let el=$('wbToast'); if (!el) { el=document.createElement('div'); el.id='wbToast'; el.setAttribute('role','status'); } const dialog=document.querySelector('.wb-drawer[open]'); el.className=dialog?'wb-feedback':'toast'; if(dialog)dialog.insertBefore(el,dialog.children[1]);else document.body.append(el); el.textContent=message; setTimeout(()=>el.remove(),5000); }
  const actual = () => window.Harbour.state();
  function rows() {
    const live = (actual()?.deals || []).map(d => {
      const v = d.versions.at(-1), r=d.requirements || {}, n=window.Harbour.next(d);
      const status=['confirmed','declined','expired'].includes(d.status) ? d.status : n.cls==='you' ? 'approval' : ['new','intake','needs_info'].includes(d.status) ? 'draft' : d.status==='handoff' ? 'unavailable' : 'proposal';
      const rooms = d.booking?.rooms || v?.terms.rooms;
      return {id:d.id, demo:false, company:d.company, contact:d.sender, arrival:r.arrival, departure:r.departure, rooms:r.rooms || (rooms ? Math.max(...Object.values(rooms)) : 0),
        status, label:({handoff:'Staff attention',declined:'Declined',expired:'Expired'})[d.status] || labels[status], total:v?.pricing.total || 0, paid:d.finance_view?.net_received || 0, payout:d.payout_view,
        nights:rooms ? Object.values(rooms).reduce((a,b)=>a+b,0) : (r.arrival && r.departure ? Math.max(0,(Date.parse(r.departure)-Date.parse(r.arrival))/864e5)*(r.rooms||0) : 0),
        next:n.short, review:['you','bad'].includes(n.cls) || ['new','intake'].includes(d.status), open:!['confirmed','declined','expired'].includes(d.status)};
    });
    return [...demo.map(d=>({...d, label:labels[d.status], total:M.pricing(d).total, nights:M.pricing(d).roomNights, next:action(d), review:['draft','unavailable','alternative_draft','approval'].includes(d.status), open:d.status!=='confirmed'})), ...live];
  }
  function metric(label, value, sub, extra='') { return `<div class="wb-kpi ${extra}"><dt>${label}</dt><dd>${value}</dd><p>${sub}</p></div>`; }
  function refresh() {
    const all=rows(), pending=all.filter(d=>d.review).length, fresh=all.filter(d=>['draft','unavailable','alternative_draft'].includes(d.status)).length;
    $('wbMetrics').innerHTML=metric('Open pipeline',money(all.filter(d=>d.open).reduce((n,d)=>n+d.total,0)),`${all.filter(d=>d.open).length} active group inquiries`)
      +metric('Group room nights',all.filter(d=>d.open||d.status==='confirmed').reduce((n,d)=>n+d.nights,0),'Across active and confirmed groups')
      +metric(`New inquiries ${pending ? `<span class="wb-badge">${pending} to review</span>` : ''}`,fresh,'A new opportunity to welcome a group','highlight')
      +metric('Deposits collected',money(all.reduce((n,d)=>n+d.paid,0)),'Includes labelled simulator deposits');
    const s=actual(); $('wbConnection').textContent=s ? `Harbour House connected · ${s.provider==='airwallex'?'Airwallex sandbox':'Simulated Harbour payments'} · ${s.runner.busy || 'Seller watching inbox'} · Simulator fixtures stay in this browser and never move money.` : 'Connecting to Harbour House… Simulator is available independently.';
    if (view==='payments') renderPayments(all); else renderPipeline(all);
  }
  function table(all) {
    const filtered=all.filter(d=>(filter==='all'||filter==='new'&&['draft','unavailable','alternative_draft'].includes(d.status)||filter===d.status) && `${d.company} ${d.contact||''}`.toLowerCase().includes(search.toLowerCase()));
    return `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>Group / organizer</th><th>Stay dates</th><th>Rooms</th><th>Proposal value</th><th>Status</th></tr></thead><tbody>${filtered.map(d=>`<tr data-open="${html(d.id)}"><td><button class="wb-inquiry" data-open="${html(d.id)}">${html(d.company)}<span class="wb-source">${html(d.demo ? `${d.channel==='phone'?'Phone':'Email'} · Simulated` : 'Harbour House · Connected')}</span></button></td><td>${date(d.arrival)} – ${date(d.departure)}</td><td>${d.rooms || '—'}</td><td>${d.total ? money(d.total) : 'Not quoted'}</td><td><span class="wb-status ${d.status}">${html(d.label)}</span><span class="wb-next">${html(d.next)}</span></td></tr>`).join('') || '<tr><td colspan="5" class="wb-empty">No inquiries match. Try another filter or simulate an inbound.</td></tr>'}</tbody></table></div>`;
  }
  function renderPipeline(all) {
    const moves=all.filter(d=>d.review).slice(0,6);
    const activity=[...demo.flatMap(d=>d.timeline.map(a=>({...a,company:d.company,id:d.id}))), ...(actual()?.activity || []).map(a=>({at:a.real_at||a.at,text:a.text||a.kind,company:'Harbour House',id:a.deal_id}))].sort((a,b)=>b.at.localeCompare(a.at)).slice(0,5);
    const stages=[['New inquiries', ['draft','unavailable','alternative_draft']],['Internal approval',['approval']],['Proposal',['proposal','alternative_sent']],['Confirmed',['confirmed']]];
    $('wbBody').innerHTML=`<div class="wb-columns"><section class="wb-panel"><div class="wb-panel-head"><h2>Group inquiries <span class="muted small">${all.length}</span></h2><span class="muted small">${all.filter(d=>d.review).length} pending action</span></div>
      <div class="wb-toolbar"><div class="wb-filters" aria-label="Pipeline filters">${[['new','New'],['all','All inquiries'],['approval','Internal approval'],['proposal','Proposal'],['confirmed','Confirmed']].map(([key,label])=>`<button data-filter="${key}" aria-pressed="${filter===key}">${label}</button>`).join('')}</div><label class="wb-search"><span class="small muted" hidden>Search inquiries</span><input id="wbSearch" type="search" aria-label="Search inquiries" placeholder="⌕  Search inquiries" value="${html(search)}"></label></div><div id="wbTable">${table(all)}</div></section>
      <section class="wb-panel"><div class="wb-panel-head"><h2>Your next moves</h2><span class="muted small">↗</span></div><div class="wb-moves">${moves.map(d=>`<button class="wb-move" data-open="${html(d.id)}"><span class="wb-move-icon" aria-hidden="true">${d.status==='approval'?'✓':'↗'}</span><span><b>${html(d.next)}</b><small>${html(d.company)}</small></span></button>`).join('') || '<p class="wb-empty">You’re all caught up.<br>We’ll keep an eye on the inbox.</p>'}</div></section></div>
      <div class="wb-bottom-grid"><section class="wb-panel"><div class="wb-panel-head"><h2>Pipeline at a glance</h2><span class="muted small">Current inquiries</span></div><div class="wb-summary">${stages.map(([label,statuses])=>{const count=all.filter(d=>statuses.includes(d.status)).length;return `<div class="wb-summary-row"><span>${label}</span><b>${count}</b></div><div class="wb-track"><span style="width:${all.length?count/all.length*100:0}%"></span></div>`;}).join('')}</div></section>
      <section class="wb-panel"><div class="wb-panel-head"><h2>Recent activity</h2><span class="muted small">Latest updates</span></div><div class="wb-summary">${activity.map(a=>`<div class="wb-summary-row"><span>${html(a.text)}<span class="wb-source">${html(a.company)}</span></span><span class="muted">${time(a.at)}</span></div>`).join('') || '<p class="wb-empty">New activity will appear here.</p>'}</div></section></div>`;
    // Update only the table while typing so keyboard focus and selection stay intact.
    $('wbSearch').oninput=e=>{search=e.target.value; $('wbTable').innerHTML=table(rows());};
  }
  function renderPayments(all) {
    $('wbBody').innerHTML=`<section class="wb-panel"><div class="wb-panel-head"><div><h2>Group deposits</h2><p class="wb-sub">Deposits are collected by the platform for Harbour House's Airwallex connected account; its share is paid out by funds split. Browser-only simulator deposits are labelled separately.</p></div></div><div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>Group</th><th>Deposit received (net)</th><th>Paid to hotel (net)</th><th>Platform fee</th><th>Source</th><th>Status</th></tr></thead><tbody>${all.map(d=>{const po=d.payout, paidOut=po && po.status!=='not_due';return `<tr data-open="${html(d.id)}"><td><button class="wb-inquiry" data-open="${html(d.id)}">${html(d.company)}</button></td><td>${money(d.paid)}</td><td>${paidOut?money(po.hotel_net):'—'}</td><td>${paidOut?money(po.platform_fee):'—'}</td><td>${d.demo?'Simulated · browser fixture':actual()?.provider==='airwallex'?'Airwallex sandbox':'Harbour simulated provider'}</td><td>${d.paid>0?(paidOut?({paid:'Paid to hotel',failed:'Payout refused',unknown:'Payout unknown',no_account:'No hotel account',pending:'Payout pending'})[po.status]||'Received':'Received'): 'Not received'}</td></tr>`}).join('') || '<tr><td colspan="6" class="wb-empty">No group deposits yet.</td></tr>'}</tbody></table></div></section>`;
  }
  function navigate(next) {
    view=next; location.hash=''; $('workbench').hidden=false;
    $('wbBreadcrumb').textContent=next==='overview'?'Overview':'Payments';
    $('wbTitle').textContent=next==='overview'?'A little more room to grow.':'Every deposit, accounted for.';
    document.querySelectorAll('[data-wb-view]').forEach(b=>{if(b.dataset.wbView===next)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    refresh();
  }
  function showDrawer(title, content, label='SIMULATED INQUIRY') {
    const dialog=$('wbDrawer');
    $('wbToast')?.remove();
    if (!dialog.open) trigger=document.activeElement;
    $('wbDrawerLabel').textContent=label;
    $('wbDrawerBody').innerHTML=`<h2 id="wbDrawerTitle" tabindex="-1">${title}</h2>${content}`;
    if(!dialog.open) dialog.showModal();
    $('wbDrawerTitle').focus();
  }
  // New inquiries go to Harbour House itself (POST /api/inbox): the seller agent works them with the real
  // rules, approvals only where a seller rule is exceeded, and real (sandbox) payments.
  const SAMPLES={
    email:full=>({from:'Jamie Chen <jamie@acme.example>', subject:`RFP | Leadership offsite | ${full?40:12} guests | 10–13 March 2027`,
      body:`Hello Group Sales, please quote ${full?20:6} king rooms for ${full?40:12} colleagues arriving 10 March and departing 13 March 2027, with breakfast each morning. No meeting room is needed. Our target is £210 per room/night including breakfast, and our total budget is £${full?'15,000':'4,500'} inclusive of taxes. Acme Demo Ltd pays the package; guests cover incidentals. Please reply by 14 January at noon London time. Thanks, Jamie, People & Culture.`}),
    phone:full=>({from:'Sophie Bennett <sophie@bennett.example>', subject:'Phone inquiry | Bennett wedding | 10–13 March 2027',
      body:`Call transcript, taken by reception. Sophie: We're getting married nearby and need ${full?20:4} king rooms for ${full?40:8} guests arriving 10 March and departing 13 March 2027. Could you include breakfast each morning? No meeting room is needed. We'd hope for around £200 per room/night including breakfast, and our total budget is £${full?'15,000':'3,000'} including taxes. The bill goes to Bennett Family Events Ltd. Reception: We'll send you a written quote by email. Thanks, Sophie`})};
  function simulator() {
    active=null;
    showDrawer('Let the inquiry come to you.',`<p class="wb-lede">Send Harbour House a group inquiry. Its seller agent picks it up within seconds: it checks rooms and rates, asks for anything missing, quotes, and asks the hotel manager only when a seller rule would be exceeded.</p>
      <label class="wb-field">Rooms<select id="wbScenario"><option value="available">Ask for rooms that are free</option><option value="unavailable">Ask for more rooms than are free</option></select></label>
      <div class="wb-channel-grid"><button class="wb-channel" data-sample="email"><span class="wb-channel-icon" aria-hidden="true">✉</span><b>Sample email RFP</b><p>A corporate leadership offsite: king rooms and breakfast, 10–13 March 2027.</p><span class="wb-channel-action">Use this inquiry →</span></button>
      <button class="wb-channel" data-sample="phone"><span class="wb-channel-icon" aria-hidden="true"><svg width="26" height="26" viewBox="0 0 24 24" fill="none"><path d="M5 3h4l2 5-3 2c1 3 3 5 6 6l2-3 5 2v4c0 2-3 3-6 2C8 19 3 14 2 7c0-2 1-4 3-4Z" stroke="currentColor" stroke-width="1.3"/></svg></span><b>Sample phone inquiry</b><p>A wedding room block, written up from a call transcript.</p><span class="wb-channel-action">Use this inquiry →</span></button></div>
      <form id="wbInbound"><h3>Or write your own</h3>
        <label class="wb-field">From<input id="wbInquiryFrom" required maxlength="200" value="Guest <guest@example.com>"></label>
        <label class="wb-field">Subject<input id="wbInquirySubject" required maxlength="200" value="Group inquiry"></label>
        <label class="wb-field">Inquiry<textarea id="wbInquiryText" rows="8" required maxlength="5000" placeholder="e.g. Please quote 6 king rooms for 12 colleagues arriving 10 March and departing 13 March 2027, with breakfast. Our budget is £4,500 inclusive of taxes. Acme Demo Ltd pays."></textarea></label>
        <p class="wb-sub">Harbour House sells twin and king rooms, breakfast and the Thames Room for stays 10–13 March 2027. Anything else is handed to hotel staff. Mention who pays and whether the budget includes taxes, or the agent will ask.</p>
        <button class="wb-primary" type="submit" id="wbSend">Send inquiry to Harbour House</button></form>`,'NEW INQUIRY');
    $('wbInbound').onsubmit=async e=>{
      e.preventDefault();
      const from=$('wbInquiryFrom').value.trim(), subject=$('wbInquirySubject').value.trim(), body=$('wbInquiryText').value.trim();
      if(!from||!body){notify('Add who it is from and the inquiry text.');return;}
      $('wbSend').disabled=true;
      try { const id=await window.Harbour.inbound(from,subject||'Group inquiry',body); $('wbDrawer').close(); refresh(); window.Harbour.open(id); }
      catch(err){ $('wbSend').disabled=false; }
    };
  }
  function open(id, review=false) {
    const d=demo.find(d=>d.id===id);
    if(!d){if(!actual()?.deals.some(d=>d.id===id)){notify('This inquiry is no longer available in this browser.');return;}trigger=document.activeElement;window.Harbour.open(id);return;}
    active=id; renderDetail(d, review);
  }
  function calendar(d) {
    const length=M.nights(d.arrival,d.departure).length;
    const availability=M.availability(d,demo), ok=availability.every(n=>n.fits);
    const days=Array.from({length:14},(_,i)=>M.plus('2027-04-10',i));
    const viable=days.filter(day=>M.fits(d,demo,day,M.plus(day,length)));
    const selected=M.nights(d.arrival,d.departure);
    return `${!ok?`<div class="wb-alert"><b>We don’t have room for the full stay.</b><p>${availability.filter(n=>!n.fits).map(n=>`${date(n.date)}: ${n.free} available / ${d.rooms} requested`).join(' · ')}. Let’s find another way to welcome this group.</p></div>`:''}
      <h3>Find a little flexibility</h3><p class="wb-sub">April 2027 · Sample inventory · Select an arrival. Every occupied night must fit; checkout does not occupy a night.</p>
      <div class="wb-calendar">${days.map(day=>{const n=M.availability(d,demo,day,M.plus(day,1))[0], fits=viable.includes(day);return `<button class="wb-night ${!n.fits?'shortage':''} ${selected.includes(day)&&ok?'selected':''} ${day===d.departure?'checkout':''}" data-date="${day}" aria-pressed="${ok&&selected.includes(day)}" aria-label="${date(day)}, ${n.free} rooms free${fits?', viable arrival':', unavailable arrival'}" ${!fits?'disabled':''}><b>${Number(day.slice(-2))}</b><small>${day===d.departure?'Checkout':n.free+' free'}</small></button>`;}).join('')}</div>
      <p class="wb-sub">Pale green: inventory available · Red: shortage · Dark green: selected occupied nights · Dashed: checkout</p>
      <div class="row" style="margin-top:14px">${[1,2].filter(n=>{const a=M.plus('2027-04-10',n);return M.fits(d,demo,a,M.plus(a,length));}).map(n=>`<button data-date="${M.plus('2027-04-10',n)}">${n} day${n===1?'':'s'} later</button>`).join('')}</div>`;
  }
  function renderDetail(d, review=false) {
    const p=M.pricing(d), approved=d.approval?.status==='approved', fits=M.fits(d,demo);
    let body=`<p class="wb-lede">${html(d.contact)} · ${d.channel==='phone'?'Phone inquiry':'Email RFP'} <span class="wb-status ${d.status}">${labels[d.status]}</span></p>
      <div class="wb-demo-note">SIMULATED · Browser-only fixture. No live inventory, manager messages or money movement.</div>
      <details><summary>Original ${d.channel==='phone'?'call transcript':'email'}</summary><pre class="wb-original">${html(d.original)}</pre></details>
      <h3>What the group needs</h3><div class="facts"><span>${d.rooms} rooms</span><span>${date(d.arrival)} – ${date(d.departure)} (checkout)</span><span>${d.breakfast?'Breakfast':'Room only'}</span>${d.meeting?'<span>Meeting room</span>':''}</div>
      <p class="wb-sub">Availability: ${fits?'every occupied night has sufficient inventory':'shortage across the requested stay'}.</p>`;
    if (['unavailable','alternative_draft','alternative_sent'].includes(d.status)) {
      body+=calendar(d);
      if(d.reply) body+=`<h3>Review alternative reply</h3><label class="wb-field">Hotel reply<textarea id="wbReply" rows="5" ${d.status==='alternative_sent'?'readonly':''}>${html(d.reply)}</textarea></label>${d.status==='alternative_draft'?'<button class="wb-primary" data-command="send-alternative">Send reviewed reply · simulated</button>':'<button class="wb-primary" data-command="accept-alternative">Simulate customer acceptance</button>'}`;
    } else {
      if(d.revisionReason) body+=`<div class="wb-alert"><b>Revision requested</b><p>${html(d.revisionReason)}</p></div>`;
      body+=`<h3>${d.status==='confirmed'?'Confirmed terms':'Prepare the proposal'}</h3><form id="wbTerms"><fieldset ${d.status==='confirmed'?'disabled':''} style="border:0;padding:0;margin:0"><div class="wb-terms">
        <label class="wb-field">Arrival<input name="arrival" type="date" value="${d.arrival}" required></label><label class="wb-field">Checkout<input name="departure" type="date" value="${d.departure}" required></label>
        <label class="wb-field">Nightly rate · GBP<input name="rate" type="number" min="100" max="1000" step="0.01" value="${d.rate/100}" required></label><div></div></div>
        <div class="row"><label><input name="breakfast" type="checkbox" ${d.breakfast?'checked':''}> Breakfast · £15 per room/night</label><label><input name="meeting" type="checkbox" ${d.meeting?'checked':''}> Meeting room · £600</label></div>
        <div class="wb-totals"><div><small>Proposal total · ${p.roomNights} room nights</small><b id="wbDraftTotal">${money(p.total)}</b></div><div><small>30% deposit</small><b id="wbDraftDeposit">${money(p.deposit)}</b></div></div>
        ${d.status!=='confirmed'?'<button type="submit">Save revised terms</button>':''}</fieldset></form>`;
      if(d.status==='draft') body+=`<h3>Get the right eyes on it</h3><p class="wb-sub">Approval is bound to these exact dates, rate and services. Saving changes requires a new approval.</p><label class="wb-field">Approver<select id="wbManager"><option>Revenue manager</option><option>General manager</option></select></label><button class="wb-primary" data-command="request-approval" ${!fits?'disabled':''}>Send link · simulated</button>`;
      if(d.approval) body+=`<h3>${approved?'Approved proposal':'Internal approval'}</h3><p class="wb-sub">${html(d.approval.manager)} · ${html(d.approval.status)}</p><a class="wb-link" href="#review/${html(d.id)}">${html(location.origin+location.pathname+'#review/'+d.id)}</a><p class="wb-sub">Simulated delivery. This link opens the manager view in this browser; it does not send a message or grant backend authority.</p>`;
      if(d.status==='approval') body+=review?`<h3>Manager review</h3><p class="wb-sub">Review the exact proposed terms above.</p><label class="wb-field">Revision reason<textarea id="wbReason" rows="2" placeholder="Required if declining"></textarea></label><div class="row"><button class="wb-primary" data-command="approve">Approve exact terms</button><button data-command="decline">Decline / request revision</button></div>`:`<button class="wb-primary" data-command="review" style="margin-top:14px">Open manager review</button>`;
      if(d.status==='proposal') body+=`<div class="wb-demo-note">Manager approval is current. Waiting for the customer to accept and pay the deposit.</div><button class="wb-primary" data-command="confirm">Simulate acceptance + 30% deposit</button>`;
      if(d.status==='confirmed') body+=`<div class="wb-demo-note"><b>Confirmed · ${money(d.paid)} simulated deposit received.</b></div><details open><summary>Operations handoff</summary><p>${d.rooms} rooms · ${date(d.arrival)} arrival · ${date(d.departure)} checkout.</p><p>${d.breakfast?'Breakfast included. ':''}${d.meeting?'Prepare the meeting room. ':''}Collect the rooming list before arrival. Balance due: ${money(p.total-d.paid)}.</p><p class="wb-sub">Demo checklist only. No task has been sent to hotel operations.</p></details>`;
    }
    body+=`<h3>Inquiry journey</h3><ol class="wb-timeline">${d.timeline.slice().reverse().map(a=>`<li><time datetime="${html(a.at)}">${new Date(a.at).toLocaleString('en-GB')}</time>${html(a.text)}</li>`).join('')}</ol>`;
    showDrawer(html(d.company),body,review?'SIMULATED MANAGER REVIEW':'SIMULATED INQUIRY');
    if($('wbTerms')) {
      $('wbTerms').onsubmit=e=>{e.preventDefault();try{applyTerms(d);save();refresh();renderDetail(d,review);notify('Terms saved. Changed terms require fresh approval.');}catch(err){notify(err.message);}};
      $('wbTerms').oninput=()=>{
        try { const draft=readTerms(d), price=M.pricing(draft); $('wbDraftTotal').textContent=money(price.total); $('wbDraftDeposit').textContent=money(price.deposit); }
        catch { $('wbDraftTotal').textContent='Check dates'; $('wbDraftDeposit').textContent='—'; }
      };
    }
  }
  function readTerms(d) {
    const form=$('wbTerms'), data=new FormData(form);
    return {...d,arrival:data.get('arrival'),departure:data.get('departure'),rate:Math.round(Number(data.get('rate'))*100),breakfast:data.has('breakfast'),meeting:data.has('meeting')};
  }
  function applyTerms(d) { const draft=readTerms(d); M.revise(d,draft); if(d.status==='draft'&&!M.fits(d,demo))d.status='unavailable'; }
  function command(command) {
    const d=demo.find(d=>d.id===active); if(!d)return;
    try {
      if(command==='review'){renderDetail(d,true);return;}
      if(command==='send-alternative') M.sendAlternative(d,$('wbReply').value);
      if(command==='accept-alternative') M.acceptAlternative(d,demo);
      if(command==='request-approval'){const manager=$('wbManager').value;applyTerms(d);M.requestApproval(d,manager,demo);}
      // Refuse unsaved edits as well as saved revisions: approval must match the visible terms.
      if(['approve','decline','confirm'].includes(command)) {
        const before=JSON.stringify([d.arrival,d.departure,d.rate,d.breakfast,d.meeting]);
        applyTerms(d);
        if(before!==JSON.stringify([d.arrival,d.departure,d.rate,d.breakfast,d.meeting])) { save();refresh();renderDetail(d);throw Error('Terms changed. Send a fresh manager approval before continuing.'); }
        if(command==='confirm')M.confirm(d,demo);else M.decide(d,command,$('wbReason')?.value||'',demo);
      }
      save(); refresh(); renderDetail(d,command==='approve'||command==='decline');
    } catch(err){notify(err.message);}
  }
  document.addEventListener('click',e=>{
    const button=e.target.closest('button,a,tr[data-open]');if(!button)return;
    if(button.dataset.wbView)navigate(button.dataset.wbView);
    if(button.hasAttribute('data-wb-close'))button.closest('dialog').close();
    if(button.dataset.filter){filter=button.dataset.filter;refresh();document.querySelector(`[data-filter="${filter}"]`).focus();}
    if(button.dataset.open)open(button.dataset.open);
    if(button.dataset.sample){const m=SAMPLES[button.dataset.sample]($('wbScenario').value==='unavailable');$('wbInquiryFrom').value=m.from;$('wbInquirySubject').value=m.subject;$('wbInquiryText').value=m.body;$('wbInquiryText').focus();}
    if(button.dataset.date){const d=demo.find(d=>d.id===active);try{M.selectAlternative(d,button.dataset.date,demo);save();refresh();renderDetail(d);}catch(err){notify(err.message);}}
    if(button.dataset.command)command(button.dataset.command);
    if(button.matches('a[href^="#review/"]')) { e.preventDefault(); open(button.hash.slice(8),true); }
  });
  for(const dialog of [$('wbDrawer'),$('liveDrawer')]) {
    dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right)dialog.close();}});
    dialog.addEventListener('close',()=>{if(trigger?.isConnected)trigger.focus();else $('wbSimulate').focus();});
  }
  $('wbSimulate').onclick=simulator;
  $('wbReset').onclick=()=>{
    showDrawer('Start fresh?', '<p class="wb-lede">Remove this browser’s simulated inquiries, approvals and deposits? Harbour House data will stay as it is.</p><button class="wb-primary" id="wbConfirmReset">Reset simulator</button>', 'RESET SIMULATOR');
    $('wbConfirmReset').onclick=()=>{demo=[];save();$('wbDrawer').close();refresh();};
  };
  $('wbAvailability').onclick=()=>{location.hash='availability';}; $('wbRules').onclick=()=>{location.hash='rules';};
  window.addEventListener('hashchange',()=>{if(location.hash.startsWith('#review/'))open(location.hash.slice(8),true);});
  // Preserve focus while Harbour polls, including keyboard search and draft entry.
  window.OpenFlow={refresh:()=>{if(document.activeElement?.closest('#workbench'))return;refresh();}};
  const mobile=matchMedia('(max-width:760px)');
  const propertyTools=()=>{document.querySelector('.wb-bottom').open=!mobile.matches;};
  propertyTools(); mobile.addEventListener('change',propertyTools);
  refresh();
  if(location.hash.startsWith('#review/'))open(location.hash.slice(8),true);
})();
