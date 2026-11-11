// Isolated loopback/simulated server. Runs booking -> PMS checkout -> incremental collection -> final folio.
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const base=process.env.FOLIO_TEST_URL||'http://127.0.0.1:8033';
if(!['localhost','127.0.0.1'].includes(new URL(base).hostname))throw Error('Use an isolated local server.');
(async()=>{
 const b=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const ctx=await b.newContext({viewport:{width:1440,height:1100}}),p=await ctx.newPage(),errors=[];
  p.on('pageerror',e=>errors.push(e.message));await p.goto(base);
  const state=()=>p.evaluate(()=>fetch('/api/state').then(r=>r.json()));assert.equal((await state()).provider,'simulated');
  await p.evaluate(()=>fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));await p.reload();
  await p.waitForFunction(()=>window.Harbour.state()?.deals.some(d=>d.company==='Summit Robotics'&&d.status==='offer_sent'));
  let d=(await state()).deals.find(d=>d.company==='Summit Robotics'),id=d.id;
  const buyer=await ctx.newPage();buyer.on('pageerror',e=>errors.push(e.message));await buyer.goto(base+d.inbox_url);
  await buyer.evaluate(async d=>{const r=await fetch('/api/buyer/'+d.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:d.buyer_token,action:'accept',payload:{version:d.versions.at(-1).n}})});if(!r.ok)throw Error(await r.text());},d);
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.status==='awaiting_payment',id);
  d=(await state()).deals.find(d=>d.id===id);
  const deposit=await ctx.newPage();await deposit.goto(d.pay_url);await deposit.getByRole('link',{name:/Pay deposit/}).click();await deposit.getByRole('button',{name:/Pay/}).click();
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.booking?.status==='confirmed',id);await deposit.close();
  await p.evaluate(id=>window.Harbour.open(id),id);await p.getByRole('button',{name:'Final folio',exact:true}).click();
  await p.locator('#folioRef').fill('PMS-DEMO-GROUP-001');await p.getByRole('button',{name:'Link reservation',exact:true}).click();
  await p.getByRole('button',{name:'Advance simulated demo to checkout',exact:true}).click();
  await p.getByRole('button',{name:'Import simulated PMS folio · £2,000 extras',exact:true}).click();
  await p.locator('[data-line="1"] [data-field=payer]').selectOption('master');
  await p.getByRole('button',{name:'Save PMS charges & allocation for review',exact:true}).click();
  // Record the reservation balance already settled through the PMS (not an invented Airwallex receipt).
  await p.getByText('Record a PMS payment already received',{exact:true}).click();
  const form=p.locator('#folioReceipt');await form.locator('[name=payment_id]').fill('PMS-RESERVATION-PAID');
  await form.locator('[name=amount]').fill('3444.00');await form.locator('[name=reference]').fill('BANK-SETTLEMENT-001');await form.locator('[name=attested]').check();
  await p.getByRole('button',{name:'Record receipt for reconciliation',exact:true}).click();
  await p.getByRole('button',{name:'Approve this final bill',exact:true}).click();
  d=(await state()).deals.find(d=>d.id===id);
  assert.equal(d.folio_summary.paid,492000);assert.equal(d.folio_summary.master_total,692000);assert.equal(d.folio_summary.balance_due,200000);
  await p.getByRole('button',{name:'Create final balance payment link',exact:true}).click();
  await p.getByRole('link',{name:'Open final balance checkout ↗',exact:true}).waitFor();
  await p.setViewportSize({width:390,height:844});assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await p.setViewportSize({width:1440,height:1100});
  await buyer.reload();await buyer.locator('#finalFolio').getByRole('link',{name:'Pay final balance ↗',exact:true}).waitFor();
  assert(!/guest-001|contribution margin|hotel floor/.test(await buyer.locator('#finalFolio').innerText()));
  const [pay]=await Promise.all([ctx.waitForEvent('page'),buyer.locator('#finalFolio').getByRole('link',{name:'Pay final balance ↗',exact:true}).click()]);
  await pay.waitForLoadState();assert.match(await pay.locator('#amount').innerText(),/£2,000/);assert.match(await pay.locator('#paidSummary').innerText(),/£4,920/);
  await pay.getByRole('link',{name:/Pay final balance/}).click();await pay.getByRole('button',{name:/Pay/}).click();
  await p.waitForFunction(id=>window.Harbour.state()?.deals.find(d=>d.id===id)?.folio_summary?.status==='finalized',id);
  await p.getByRole('link',{name:'Open final folio / print or save PDF ↗',exact:true}).waitFor();
  await p.evaluate(()=>document.getElementById('folioWorkspace').scrollIntoView({block:'start'}));await p.screenshot({path:'/tmp/folio-reconciled.png'});
  await buyer.reload();const [doc]=await Promise.all([ctx.waitForEvent('page'),buyer.getByRole('link',{name:'Open final folio / save PDF ↗',exact:true}).click()]);
  await doc.waitForLoadState();assert.match(await doc.locator('body').innerText(),/Final master folio/);assert.match(await doc.locator('body').innerText(),/£6,920/);assert.match(await doc.locator('body').innerText(),/Master balance: £0/);
  const count=(await state()).ledger.filter(x=>x.deal_id===id&&x.type==='folio_received').length;
  await pay.reload();assert.equal((await state()).ledger.filter(x=>x.deal_id===id&&x.type==='folio_received').length,count);
  assert.deepEqual(errors,[]);console.log('PASS: reservation/deposit, PMS binding, checkout, charge allocation, prior payment reconciliation, exactly £2,000 incremental collection, final folio/PDF, privacy, mobile and retry.');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exit(1)});
