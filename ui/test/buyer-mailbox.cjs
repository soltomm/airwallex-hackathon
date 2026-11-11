// Local simulated server; creates a generated inquiry and exercises the buyer-mailbox action.
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const base=process.env.MAILBOX_TEST_URL||'http://127.0.0.1:8030';
if(!['127.0.0.1','localhost'].includes(new URL(base).hostname))throw Error('Use an isolated local server.');
(async()=>{
 const b=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const ctx=await b.newContext({viewport:{width:1440,height:1000}}),p=await ctx.newPage(),errors=[];
  p.on('pageerror',e=>errors.push(e.message));await p.goto(base);
  await p.waitForFunction(()=>window.Harbour?.state()?.deals.length);
  assert.equal(await p.evaluate(()=>window.Harbour.state().provider),'simulated');
  await p.locator('#wbSimulate').click();await p.getByRole('button',{name:'By you, in the buyer’s mailbox',exact:true}).click();
  await p.locator('[data-preset]').first().click();await p.locator('#liveDrawer[open]').waitFor();
  const link=p.locator('.buyer-mailbox-callout').getByRole('link',{name:'Open buyer’s mailbox ↗',exact:true});
  await link.waitFor();
  for(const width of [1440,390]){
   await p.setViewportSize({width,height:1000});await link.scrollIntoViewIfNeeded();
   const box=await link.boundingBox();assert(box.height>=44);
   const colors=await link.evaluate(el=>({bg:getComputedStyle(el).backgroundColor,fg:getComputedStyle(el).color}));
   assert.notEqual(colors.bg,'rgba(0, 0, 0, 0)');assert.notEqual(colors.bg,colors.fg);
   assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  const [buyer]=await Promise.all([ctx.waitForEvent('page'),link.click()]);await buyer.waitForLoadState();
  assert.match(buyer.url(),/\/inbox\//);assert.match(await buyer.locator('body').innerText(),/email thread with/);
  // The action must remain visible even before there is a buyer-turn Next move card.
  await p.route('**/api/state',async route=>{const response=await route.fetch(),s=await response.json();for(const d of s.deals){d.status='new';d.unread=true;}await route.fulfill({response,json:s});});
  await p.evaluate(()=>window.Harbour.reload());assert.equal(await link.isVisible(),true);
  await p.setViewportSize({width:1440,height:1000});await p.locator('#deal .title').scrollIntoViewIfNeeded();
  await p.screenshot({path:'/tmp/buyer-mailbox-prominent.png'});
  await p.reload();await p.waitForFunction(()=>window.Harbour?.state()?.deals.length);
  const id=await p.evaluate(()=>window.Harbour.state().deals.find(d=>d.generated).id);await p.evaluate(id=>window.Harbour.open(id),id);
  assert.equal(await link.isVisible(),true);assert.deepEqual(errors,[]);
  console.log('PASS: By you selection, prominent mailbox action, desktop/mobile, working new-tab link, pre-response visibility and reload.');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exit(1)});
