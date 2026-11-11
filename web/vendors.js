/* Backend-owned third-party workflow UI. Contacts and deliveries are fictional fixtures. */
(() => {
  const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const gbp=n=>new Intl.NumberFormat('en-GB',{style:'currency',currency:'GBP'}).format(n/100);
  const pounds=s=>{if(!/^\d+(\.\d{1,2})?$/.test(s))throw Error('Enter GBP with up to two decimal places.');const [a,b='']=s.split('.');return Number(a)*100+Number(b.padEnd(2,'0'));};
  let dir, current, data, error='', backFocus, busy=false, parentView='overview';
  const page=document.createElement('section');page.id='vendorPage';page.className='wb-content';page.hidden=true;document.body.append(page);
  const api=async(path,body)=>{const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw Error(j.error||'Vendor workflow failed.');return j;};
  async function directory(){return dir||(dir=await api('/api/vendor_directory'));}
  const provider=id=>dir.providers.find(v=>v.id===id);
  const title=r=>r.vendor_unavailable?'Source an alternative provider':({awaiting_quote:'Waiting on vendor quote',quoted:'RevOps review required',revision_requested:'Internal revision required',awaiting_confirmation:'Waiting on vendor confirmation',confirmed:'Vendor confirmed',in_progress:'Delivery in progress',delivered:'Delivered',cancelled:'Cancelled'})[r.status];
  async function act(command,payload={}){
    if(busy)return; const actionId=current;busy=true;page.querySelectorAll('button').forEach(b=>b.disabled=true);error='';
    try{
      let result=await api(`/api/deals/${actionId}/vendors`,{command,payload});
      await window.Harbour.reload();
      if(command==='propose')result=await api(`/api/deals/${actionId}/vendors`);
      if(location.hash!=='#vendors/'+actionId)return;
      data=result;render();
    }catch(e){if(location.hash==='#vendors/'+actionId){error=e.message;render();}}
    finally{busy=false;}
  }
  function show(){document.querySelectorAll('#workbench,#avail,#rules').forEach(x=>x.hidden=true);$('liveDrawer').open&&$('liveDrawer').close();page.hidden=false;}
  async function route(){
    const hash=location.hash;
    if(!hash.startsWith('#vendors/')&&hash!=='#directory'){page.hidden=true;$('vendorDirectoryNav').removeAttribute('aria-current');const selected=document.querySelector('[data-wb-view][aria-current]')?.dataset.wbView||parentView;document.querySelector(`[data-wb-view="${selected}"]`)?.setAttribute('aria-current','page');$('wbBreadcrumb').textContent=selected==='payments'?'Payments':'Overview';return;}
    if(page.hidden)parentView=document.querySelector('[data-wb-view][aria-current]')?.dataset.wbView||parentView;
    $('wbBreadcrumb').textContent=hash==='#directory'?'Directory':'Third-party workspace';
    $('vendorDirectoryNav').toggleAttribute('aria-current',false);
    if(hash==='#directory'){$('vendorDirectoryNav').setAttribute('aria-current','page');document.querySelectorAll('[data-wb-view]').forEach(b=>b.removeAttribute('aria-current'));}
    show();page.innerHTML='<p role="status">Loading connections…</p>';
    try{await directory();if(location.hash!==hash)return;if(hash==='#directory'){renderDirectory();return;}const id=decodeURIComponent(hash.slice(9));const result=await api(`/api/deals/${encodeURIComponent(id)}/vendors`);if(!window.Harbour.state())await window.Harbour.reload();if(location.hash!==hash)return;current=id;data=result;error='';render();$('vendorPageTitle').focus();}
    catch(e){page.innerHTML=`<p role="alert">${esc(e.message)}</p><a href="#">Back to overview</a>`;}
  }
  function renderDirectory(){
    page.innerHTML=`<div class="wb-heading"><div><h1 id="vendorPageTitle" tabindex="-1">Your people. Your partners.</h1><p>Internal team and preferred third-party connections.</p></div><a href="#">Back to overview</a></div><p class="wb-demo-note">Fictional demo directory · Reserved example addresses. No messages or invitations are sent.</p><label class="wb-field">Search contacts<input id="vendorContactSearch" type="search" placeholder="Name, category or role"></label><div id="vendorContacts"></div>`;
    const draw=()=>{const q=$('vendorContactSearch').value.toLowerCase();$('vendorContacts').innerHTML=`<h2>Internal team</h2><div class="vendor-grid">${dir.team.filter(p=>JSON.stringify(p).toLowerCase().includes(q)).map(p=>`<article class="card"><b>${esc(p.name)}</b><p>${esc(p.role)}</p><p>${esc(p.email)}</p></article>`).join('')||'<p>No matching team members.</p>'}</div><h2>External providers</h2><div class="vendor-grid">${dir.providers.filter(p=>JSON.stringify(p).toLowerCase().includes(q)).map(p=>`<article class="card"><b>${esc(p.name)}</b><p>${p.categories.map(esc).join(' · ')}</p><p>${esc(p.contact)}<br>${esc(p.email)}<br>${esc(p.phone)}</p><p class="small muted">${esc(p.location)} · Simulated capacity: ${Object.entries(p.capacity).map(([k,v])=>`${v} ${esc(k)}`).join(', ')} per day</p></article>`).join('')||'<p>No matching providers.</p>'}</div>`;};
    $('vendorContactSearch').oninput=draw;draw();$('vendorPageTitle').focus();
  }
  function render(){
    const w=data.workspace,req=data.requirements||{}, preview=data.preview,p=preview?.pricing;
    page.innerHTML=`<div class="wb-heading"><div><h1 id="vendorPageTitle" tabindex="-1">${esc(data.company)} · Third-party workspace</h1><p>Source, review and coordinate the complete group package.</p></div><button id="vendorBack">Back to inquiry</button></div>
      <div class="wb-demo-note">Simulated delivery and provider confirmations · Saved to this inquiry on the server. Vendor payment status is tracking only; no payout or legal signing occurs.</div>
      ${error?`<p class="vendor-error" role="alert">${esc(error)}</p>`:''}
      <div class="wb-columns"><div><section class="card"><h2>Scope & allocation</h2><form id="vendorConfig"><label><input id="vendorEnabled" type="checkbox" ${w.enabled?'checked':''}> Needs third-party management</label><div class="wb-terms" style="margin-top:16px"><label class="wb-field">Host hotel rooms per night<input id="vendorHostRooms" type="number" min="0" max="${req.rooms||0}" value="${w.host_rooms??req.rooms??0}" required></label><label class="wb-field">External meeting rooms required<input id="vendorMeetings" type="number" min="0" max="20" value="${w.external_meetings}" required></label></div><p class="small muted">Requested: ${req.rooms??'unknown'} rooms · ${esc(req.arrival||'dates pending')} to ${esc(req.departure||'dates pending')}. Partner rooms fill the remainder. External meeting coverage replaces the hotel meeting room.</p><button class="wb-primary">Save scope</button></form></section>
      ${w.enabled?`<section class="card"><h2>Source a connection</h2><form id="vendorRequest"><div class="wb-terms"><label class="wb-field">Service<select id="vendorCategory">${['rooms','meeting','catering','av','technician','amenities'].map(c=>`<option>${c}</option>`).join('')}</select></label><label class="wb-field">Preferred provider<select id="vendorProvider"></select></label></div><label class="wb-field">Requested scope<input id="vendorTitle" placeholder="e.g. 3 classroom spaces with AV" maxlength="180" required></label><div class="wb-terms"><label class="wb-field">Start<input type="date" id="vendorStart" value="${esc(req.arrival||'')}" required></label><label class="wb-field">End / checkout (exclusive)<input type="date" id="vendorEnd" value="${esc(req.departure||'')}" required></label><label class="wb-field">Quantity<input type="number" id="vendorQuantity" min="1" max="1000" value="1" required></label><label class="wb-field">Covers an unsupported requirement<select id="vendorCovers"><option value="">None / general service</option></select></label></div><p class="small muted">Rooms/meeting quantity is per occupied day; catering quantity is people for the complete meal package; other categories use complete-service units. Your quote request is recorded with simulated delivery.</p><button class="wb-primary">Issue sub-RFP · simulated</button></form></section>
      <h2>Requests & subcontracts</h2>${w.requests.map(requestCard).join('')||'<p class="wb-empty">No vendor requests yet.</p>'}`:''}</div>
      <aside><section class="card"><h2>${data.accepted?'Agreed master contract':'Master estimate'}</h2><p class="small muted">${data.accepted?'Accepted master terms are read-only. Vendor delivery and payment tracking remain available.':'PROVISIONAL until vendor confirmations, hotel approval and customer acceptance are current. This estimate includes only priced lines; unquoted services are excluded.'}</p>${p?`<div class="vendor-total">${gbp(p.total)}</div><p>Deposit: ${gbp(p.deposit)} · ${p.deposit_pct}%</p><table class="t">${p.lines.map(l=>`<tr><td>${esc(l.item)}</td><td class="r">${gbp(l.amount)}</td></tr>`).join('')}</table><p class="small muted">Internal contribution margin: ${p.margin_all_rooms.margin_pct}% / ${p.margin_full_release.margin_pct}% at full release. Hotel floor still applies.</p>`:'<p>Complete stay requirements to price the hotel portion.</p>'}
      ${(data.problems||[]).map(x=>`<p class="small vendor-warning">${esc(x)}</p>`).join('')}${preview?.policy.hard.map(x=>`<p class="small vendor-warning">${esc(x)}</p>`).join('')||''}${preview&&!preview.fits_hotel_inventory?'<p class="small vendor-warning">The host allocation does not fit hotel inventory.</p>':''}
      ${w.enabled?`<div class="vendor-actions"><button id="vendorProvisional" ${!p||data.accepted?'disabled':''}>Save & share provisional estimate</button><button id="vendorMaster" class="wb-primary" ${data.accepted||!p||data.problems.length||!preview.policy.ok||!preview.fits_hotel_inventory?'disabled':''}>Submit master proposal for hotel approval</button></div><p class="small muted">Provisional sharing adds a non-bookable estimate to the buyer portal, with unpriced items identified. It sends no email. Final approval is bound to the full package.</p>`:''}
      <p class="small muted">Vendor services are guaranteed outside automatic hotel-room attrition. Post-acceptance financial changes require a separate amendment.</p></section>
      <section class="card"><h2>Internal & external next steps</h2>${w.requests.filter(r=>r.status!=='cancelled').map(r=>`<p><b>${esc(r.title)}</b><br><span class="small muted">${esc(title(r))}</span></p>`).join('')||'<p>No open requests.</p>'}</section></aside></div>`;
    if(data.accepted)page.querySelectorAll('#vendorConfig input,#vendorConfig button,#vendorRequest input,#vendorRequest select,#vendorRequest button').forEach(el=>el.disabled=true);
    $('vendorBack').onclick=async()=>{location.hash='';await window.Harbour.reload();window.Harbour.open(current);};
    $('vendorConfig').onsubmit=e=>{e.preventDefault();act('configure',{enabled:$('vendorEnabled').checked,host_rooms:$('vendorHostRooms').value,external_meetings:$('vendorMeetings').value});};
    if(!w.enabled)return;
    const fillProviders=()=>{const c=$('vendorCategory').value;if(c==='catering')$('vendorQuantity').value=req.guests||req.rooms||1;$('vendorProvider').innerHTML=dir.providers.filter(p=>p.categories.includes(c)).map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');const covers={catering:['breakfast','group dinner','lunch','drinks reception'],amenities:['airport transfers','transfers','spa','parking','coach transport']}[c]||[];$('vendorCovers').innerHTML='<option value="">None / general service</option>'+covers.filter(t=>(req.unsupported||[]).includes(t)||(t==='breakfast'&&req.breakfast)).map(t=>`<option>${esc(t)}</option>`).join('');};
    $('vendorCategory').onchange=fillProviders;fillProviders();
    $('vendorRequest').onsubmit=e=>{e.preventDefault();act('request',{vendor_id:$('vendorProvider').value,category:$('vendorCategory').value,title:$('vendorTitle').value,start:$('vendorStart').value,end:$('vendorEnd').value,quantity:$('vendorQuantity').value,covers:$('vendorCovers').value?[$('vendorCovers').value]:[]});};
    $('vendorProvisional').onclick=()=>act('provisional');$('vendorMaster').onclick=()=>act('propose');
    for(const form of page.querySelectorAll('[data-quote-form]'))form.onsubmit=e=>{e.preventDefault();try{const f=new FormData(form);act('quote',{request_id:form.dataset.quoteForm,unit_cost:pounds(f.get('unit_cost')),markup_type:f.get('markup_type'),markup:f.get('markup_type')==='flat'?pounds(f.get('markup')):f.get('markup'),valid_until:f.get('valid_until')+':00Z'});}catch(e){error=e.message;render();}};
    page.querySelectorAll('[data-vendor-action]').forEach(b=>b.onclick=()=>{const r=w.requests.find(r=>r.id===b.dataset.id);act(b.dataset.vendorAction,{request_id:r.id,quote_hash:r.quote?.hash,reason:page.querySelector(b.dataset.vendorAction==='unavailable'?`[data-unavailable="${r.id}"]`:`[data-reason="${r.id}"]`)?.value||'',status:b.dataset.status,note:page.querySelector(`[data-note="${r.id}"]`)?.value||''});});
  }
  function requestCard(r){
    const q=r.quote,locked=data.accepted;
    const button=(command,label,status)=>`<button type="button" data-vendor-action="${command}" data-id="${r.id}" ${status?`data-status="${status}"`:''}>${label}</button>`;
    return `<article class="card"><div class="label"><span>${esc(provider(r.vendor_id)?.name)}</span><span class="chip">${esc(title(r))}</span></div><h3>${esc(r.title)}</h3><p class="small muted">${r.quantity} ${esc(r.category)} · ${esc(r.start)} to ${esc(r.end)} · Owner: Revenue operations</p>
      ${q?`<p>Unit cost: ${gbp(q.unit_cost)} · ${q.markup_type==='percent'?q.markup+'%':gbp(q.markup)} markup · Quote revision ${q.revision}</p><p class="small muted">Valid until ${esc(q.valid_until)}. Supplier costs and markup are internal.</p>`:''}
      ${!locked&&r.status!=='cancelled'?`<details><summary>${q?'Revise vendor quote':'Record vendor quote'} · simulated</summary><form data-quote-form="${r.id}"><label class="wb-field">GBP unit cost<input name="unit_cost" type="text" inputmode="decimal" value="${q?(q.unit_cost/100).toFixed(2):''}" required></label><div class="wb-terms"><label class="wb-field">Markup type<select name="markup_type"><option value="percent" ${q?.markup_type==='percent'?'selected':''}>Whole percentage</option><option value="flat" ${q?.markup_type==='flat'?'selected':''}>Flat GBP per sub-item</option></select></label><label class="wb-field">Markup value<input name="markup" type="text" value="${q?(q.markup_type==='flat'?(q.markup/100).toFixed(2):q.markup):20}" required></label></div><label class="wb-field">Quote expiry (UTC)<input name="valid_until" type="datetime-local" value="${q?q.valid_until.slice(0,16):new Date(Date.parse(window.Harbour.state().clock)+864e5).toISOString().slice(0,16)}" required></label><button>Save quote revision</button></form></details>`:''}
      ${!locked&&r.status!=='cancelled'?`<label class="wb-field">Provider availability note<input data-unavailable="${r.id}" placeholder="Reason if the provider cannot fulfill"></label>`:''}
      <div class="vendor-actions">${!locked&&r.status!=='cancelled'?button('unavailable','Record vendor unavailable · simulated'):''}${r.status==='quoted'?`<input data-reason="${r.id}" aria-label="Revision reason for ${esc(r.title)}" placeholder="Reason if requesting revision">${button('approve','RevOps approve exact quote')}${button('reject','Request revision')}`:''}${r.status==='awaiting_confirmation'?button('confirm','Simulate vendor confirmation'):''}${!locked&&r.status!=='cancelled'?button('cancel','Cancel request'):''}</div>
      ${r.review_note?`<p>${esc(r.review_note)}</p>`:''}${locked&&['confirmed','in_progress','delivered'].includes(r.status)?`<label class="wb-field">Execution note<input data-note="${r.id}" value="${esc(r.execution_note)}"></label><div class="vendor-actions">${button('execution','Mark in progress','in_progress')}${button('execution','Mark delivered','delivered')}${button('payment_status','Track scheduled payment','scheduled')}${button('payment_status','Mark payment recorded','paid')}</div><p class="small muted">Payment: ${esc(r.payment_status)} · Tracking only, no transfer.</p>`:''}</article>`;
  }
  function mount(d){
    const block=document.createElement('section');block.className='card';block.innerHTML=`<div class="label">Third-party management</div><label><input type="checkbox" id="vendorInquiryToggle" ${d.third_party?.enabled?'checked':''} ${d.accepted_version?'disabled':''}> Needs third-party management</label><p class="small muted">Source partner space, catering, AV and support under this inquiry’s master proposal.</p><button id="vendorInquiryOpen">Manage connections & subcontracts</button>`;
    $('vendorInquiryCard')?.remove();block.id='vendorInquiryCard';const next=$('deal').querySelector('.next');$('deal').insertBefore(block,next?next.nextSibling:$('deal').children[1]||null);
    const open=()=>{backFocus=document.activeElement;location.hash='vendors/'+d.id;};
    $('vendorInquiryToggle').onchange=async e=>{try{await api(`/api/deals/${d.id}/vendors`,{command:'configure',payload:{enabled:e.target.checked,host_rooms:d.third_party?.host_rooms??d.requirements?.rooms??null,external_meetings:d.third_party?.external_meetings??0}});await window.Harbour.reload();if(e.target.checked)open();}catch(err){e.target.checked=!e.target.checked;block.querySelector('p').textContent=err.message;}};
    $('vendorInquiryOpen').onclick=open;
  }
  function nextMoves(all){
    const deals=window.Harbour.state()?.deals||[],tasks=[];
    for(const d of deals)for(const r of d.third_party?.enabled?d.third_party.requests:[]){
      if(r.status==='cancelled'||r.status==='delivered'&&r.payment_status==='paid')continue;
      if(r.status==='confirmed'&&!d.booking&&(d.accepted_version||d.versions.some(v=>['pending_approval','approved','sent'].includes(v.status))))continue;
      const external=['awaiting_quote','awaiting_confirmation','in_progress'].includes(r.status);
      const text=r.status==='confirmed'?(d.booking?'Operations handoff needed':'Prepare master proposal'):r.status==='delivered'?'Track vendor payment':r.status==='in_progress'?'Waiting on vendor delivery':title(r);
      tasks.push({deal:d,id:r.id,external,text,name:r.title});
    }
    const task=t=>`<button class="wb-move" data-vendor-open="${esc(t.deal.id)}"><span><b>${esc(t.text)}</b><small>${esc(t.deal.company)} · ${esc(t.name)}</small></span></button>`;
    const move=(d,cls)=>`<button class="wb-move ${cls}" data-open="${esc(d.id)}"><span class="wb-move-icon" aria-hidden="true">${cls==='you'?'!':'↗'}</span><span><b>${esc(d.next)}</b><small>${esc(d.company)}${d.ref?' · '+esc(d.ref):''}</small></span></button>`;
    const base=all.filter(d=>d.review).map(d=>move(d,'you')).join('');
    const buyer=all.filter(d=>d.turn==='buyer').slice(0,3).map(d=>move(d,'buyer')).join('');
    return `<h3 class="vendor-lane">Needs you · hotel manager</h3>${base+tasks.filter(t=>!t.external).map(task).join('')||'<p class="small muted">Nothing waiting on you.</p>'}${tasks.some(t=>t.external)?'<h3 class="vendor-lane">Waiting on external providers</h3>'+tasks.filter(t=>t.external).map(task).join(''):''}${buyer?'<h3 class="vendor-lane">Waiting on buyers</h3>'+buyer:''}`;
  }
  document.addEventListener('click',e=>{const b=e.target.closest('[data-vendor-open]');if(b)location.hash='vendors/'+b.dataset.vendorOpen;});
  const pendingIds=()=> (window.Harbour.state()?.deals||[]).filter(d=>d.third_party?.enabled&&d.third_party.requests.some(r=>['quoted','revision_requested'].includes(r.status)||r.status==='delivered'&&r.payment_status!=='paid'||r.status==='confirmed'&&(d.booking||!d.accepted_version&&!d.versions.some(v=>['pending_approval','approved','sent'].includes(v.status))))).map(d=>d.id);
  $('vendorDirectoryNav').onclick=()=>{location.hash='directory';};window.addEventListener('hashchange',route);
  window.Vendors={mount,nextMoves,pendingIds};route();
})();
