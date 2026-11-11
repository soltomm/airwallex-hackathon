/* Post-stay reconciliation is owned by the engine. These controls only submit reviewed PMS inputs. */
(()=>{
 const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const money=n=>n==null?'—':new Intl.NumberFormat('en-GB',{style:'currency',currency:'GBP'}).format(n/100);
 const pence=s=>{if(!/^-?\d+(\.\d{1,2})?$/.test(s))throw Error('Enter GBP amounts with up to two decimals.');const neg=s.startsWith('-'),[a,b='']=s.replace('-','').split('.');return (neg?-1:1)*(Number(a)*100+Number(b.padEnd(2,'0')));};
 const categories=['accommodation','minibar','food_beverage','parking','incidentals','adjustment'];
 let current,data,draft,error='',busy=false,versionKey;
 function mount(d){
  if(!$('folioWorkspace'))return;
  if(!d.folio_summary){$('folioWorkspace').innerHTML='<p>Confirm a booking before reconciling the final folio.</p>';return;}
  current=d.id;data=d.folio_summary;
  const key=d.id+':'+data.snapshot_hash;
  if(versionKey!==key){versionKey=key;draft=structuredClone(data.snapshot?.lines||[{posting_id:'reservation',category:'accommodation',description:'Agreed reservation package',payer:'master',guest_ref:null,amount:data.reservation_total}]);error='';}
  render();
 }
 const option=(values,selected)=>values.map(v=>`<option value="${v}" ${v===selected?'selected':''}>${v.replaceAll('_',' ')}</option>`).join('');
 function render(){
  if(!$('folioWorkspace'))return;
  const v=data,locked=!!v.document,early=!v.checkout_reached;
  $('folioWorkspace').innerHTML=`<h2>Post-stay reconciliation</h2><p>Bridge the agreed reservation to the final PMS bill, then collect only the remaining approved master balance.</p><p class="small">Reservation <b>${esc(v.reservation_ref)}</b> · ${esc(v.status.replaceAll('_',' '))}</p><p role="alert">${esc(error)}</p>
   <div class="tiles"><div>Reservation price<div class="v">${money(v.reservation_total)}</div></div><div>Final bill · all payers<div class="v">${money(v.final_total)}</div></div><div>Difference<div class="v">${money(v.variance)}</div></div><div>Master balance due<div class="v">${money(v.balance_due)}</div></div></div>
   <p>Master charges: ${money(v.master_total)} · Airwallex receipts less refunds: ${money(v.airwallex_paid)} · External PMS receipts (staff attested): ${money(v.external_paid)}.</p><p>Guest charges: ${money(v.guest_total)} · Guest balances: ${Object.entries(v.guest_balances).map(([id,n])=>esc(id)+' '+money(n)).join(', ')||'none'}. Personal charges are excluded from the organizer’s collection.</p>
   ${v.credit_due?`<p role="alert">Credit due: ${money(v.credit_due)}. Route to finance for refund reconciliation; no new payment will be collected.</p>`:''}
   ${v.blocks.map(x=>`<p class="small">${esc(x)}</p>`).join('')}
   ${early?`<button data-folio="demo_checkout">Advance simulated demo to checkout</button><p class="small muted">Moves the demo clock for all inquiries. Available with simulated payments only.</p>`:''}
   ${!v.linked&&!locked?`<form id="folioLink"><label class="small" for="folioRef">PMS reservation number</label><input id="folioRef" value="${esc(v.reservation_ref)}" maxlength="80" required><button>Link reservation</button></form>`:''}
   ${locked?`<p><a href="${documentUrl()}" target="_blank" rel="noopener">Open final folio / print or save PDF ↗</a></p><p><a href="/api/deals/${encodeURIComponent(current)}/folio/report" target="_blank" rel="noopener">Open complete reconciled PMS folio · hotel copy ↗</a></p><p class="small">${esc(v.document.number)} · Issued ${esc(v.document.issued_at)}. This issued statement is immutable.</p>`:`
   <p class="small muted">Demo PMS data is simulated. The import API supports a future PMS adapter; this environment has no live PMS connection.</p>
   <button data-folio="simulate" ${early?'disabled':''}>Import simulated PMS folio · £2,000 extras</button>
   <form id="folioImport"><h3>Charges & payer allocation</h3><label class="small" for="folioSource">PMS folio number</label><input id="folioSource" value="${esc(v.snapshot?.source_id||'DEMO-'+v.reservation_ref)}" ${v.snapshot?'readonly':''} maxlength="100" required>
   <p class="small">Complete, tax-inclusive GBP amounts. A new incidental defaults to the individual guest. Assign it to the master only when the organizer is responsible.</p>
   <div id="folioLines">${draft.map((l,i)=>`<fieldset data-line="${i}" style="margin:12px 0;border:1px solid var(--line);border-radius:8px"><legend>Charge ${i+1} · ${esc(l.posting_id)}</legend><label class="small">Description<input data-field="description" value="${esc(l.description)}" maxlength="200" required></label><div class="row"><label class="small">Category<select data-field="category">${option(categories,l.category)}</select></label><label class="small">GBP amount<input data-field="amount" inputmode="decimal" value="${(l.amount/100).toFixed(2)}" required></label><label class="small">Bill to<select data-field="payer">${option(['master','guest'],l.payer)}</select></label><label class="small">Guest reference<input data-field="guest_ref" value="${esc(l.guest_ref||'')}" maxlength="80" placeholder="Required for guest charges"></label></div><button type="button" data-remove-line="${i}">Remove charge</button></fieldset>`).join('')}</div>
   <div class="row"><button type="button" id="folioAdd">Add incidental charge</button><button ${early?'disabled':''}>Save PMS charges & allocation for review</button></div></form>
   ${v.snapshot?`<p>PMS revision ${v.snapshot.source_revision} · ${v.approved?'Approved':'Review required'}. Check the complete bill, payer allocation and receipt evidence before approving.</p><div class="row"><button data-folio="approve" ${v.approved?'disabled':''}>Approve this final bill</button><button data-folio="checkout" ${!v.approved||v.blocks.length||!v.balance_due?'disabled':''}>Create final balance payment link</button><button data-folio="check">Check existing payment</button><button data-folio="cancel_checkout">Cancel unpaid checkout to revise bill</button><button data-folio="finalize" ${v.status!=='settled'?'disabled':''}>Issue settled final folio</button></div>`:''}
   ${v.payment_url?`<p><a href="${esc(v.payment_url)}" target="_blank" rel="noopener">Open final balance checkout ↗</a></p><p class="small">Customer consent on the hosted payment page is required. This is not an automatic card charge.</p>`:''}
   ${v.snapshot?`<details><summary>Record a PMS payment already received</summary><p class="small">Use this for a prior settlement or guest receipt, not to mark a newly created Airwallex checkout as paid. Airwallex payments are verified against the provider.</p><form id="folioReceipt"><label class="small">PMS receipt ID<input name="payment_id" required maxlength="100"></label><label class="small">GBP amount<input name="amount" inputmode="decimal" required></label><label class="small">Method<select name="method"><option value="external">External receipt · staff attestation</option><option value="airwallex">Already verified Airwallex receipt</option></select></label><label class="small">Receipt / Airwallex intent reference<input name="reference" required maxlength="160"></label><label class="small">Paid by<select name="payer"><option value="master">Master payer</option><option value="guest">Individual guest</option></select></label><label class="small">Guest reference (if personal)<input name="guest_ref" maxlength="80"></label><label><input type="checkbox" name="attested"> I have checked the external receipt evidence in the PMS.</label><button>Record receipt for reconciliation</button></form></details>`:''}`}
   <h3>Reconciled receipt ledger</h3>${(v.receipts||[]).map(r=>`<p class="small">${money(r.amount)} · ${esc(({deposit_received:'Verified reservation payment',folio_received:'Verified final balance payment',folio_external_received:'External PMS receipt · staff attested',refund_completed:'Completed refund'})[r.type])} · ${esc(r.provider_id)}</p>`).join('')}<h3>PMS receipt evidence</h3>${v.receipt_journal.map(r=>`<p class="small">${esc(r.payment_id)} · ${money(r.amount)} · ${esc(r.payer)} · ${r.method==='external'?'Staff-attested external receipt':'Matched verified Airwallex receipt'} · ${esc(r.reference)}</p>`).join('')||'<p class="small">Existing Airwallex reservation payments are included automatically; do not enter them as external receipts.</p>'}`;
  wire();
 }
 function documentUrl(){const d=window.Harbour.state().deals.find(x=>x.id===current);return `/folio/${encodeURIComponent(d.ref)}/document?t=${encodeURIComponent(d.buyer_token)}`;}
 function readDraft(){document.querySelectorAll('#folioLines [data-line]').forEach(el=>{const row=draft[Number(el.dataset.line)];for(const f of el.querySelectorAll('[data-field]'))row[f.dataset.field]=f.dataset.field==='amount'?pence(f.value):f.value;row.guest_ref=row.payer==='guest'?row.guest_ref:null;});}
 async function act(command,payload={}){
  if(busy)return;busy=true;const id=current;error='';$('folioWorkspace').querySelectorAll('button').forEach(b=>b.disabled=true);
  try{const r=await fetch(`/api/deals/${encodeURIComponent(id)}/folio`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,payload})});const j=await r.json();if(!r.ok)throw Error(j.error||'Folio action failed.');data=j;versionKey=null;await window.Harbour.reload();window.Harbour.open(id);}
  catch(e){error=e.message;render();}finally{busy=false;}
 }
 function wire(){
  document.querySelectorAll('[data-folio]').forEach(b=>b.onclick=()=>act(b.dataset.folio,{snapshot_hash:data.snapshot_hash}));
  if($('folioLink'))$('folioLink').onsubmit=e=>{e.preventDefault();act('link',{reservation_ref:$('folioRef').value});};
  if($('folioImport'))$('folioImport').onsubmit=e=>{e.preventDefault();try{readDraft();act('import',{reservation_ref:data.reservation_ref,source_id:$('folioSource').value,source_revision:(data.snapshot?.source_revision||0)+1,currency:'GBP',lines:draft,payments:[]});}catch(e){error=e.message;render();}};
  if($('folioAdd'))$('folioAdd').onclick=()=>{try{readDraft();draft.push({posting_id:'charge-'+crypto.randomUUID(),category:'incidentals',description:'Additional service',amount:1000,payer:'guest',guest_ref:'guest-001'});render();}catch(e){error=e.message;render();}};
  document.querySelectorAll('[data-remove-line]').forEach(b=>b.onclick=()=>{try{readDraft();draft.splice(Number(b.dataset.removeLine),1);render();}catch(e){error=e.message;render();}});
  if($('folioReceipt'))$('folioReceipt').onsubmit=e=>{e.preventDefault();try{const f=new FormData(e.target);act('receipt',{receipt:{payment_id:f.get('payment_id'),amount:pence(f.get('amount')),method:f.get('method'),reference:f.get('reference'),payer:f.get('payer'),guest_ref:f.get('payer')==='guest'?f.get('guest_ref'):null},external_payments_attested:f.get('attested')==='on'});}catch(e){error=e.message;render();}};
 }
 window.Folios={mount};const initial=$('folioWorkspace');if(initial){const d=window.Harbour.state()?.deals.find(x=>x.id===initial.dataset.folioDeal);if(d)mount(d);}
})();
