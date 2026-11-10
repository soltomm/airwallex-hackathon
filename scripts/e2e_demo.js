// End-to-end check of the demo script in GROUP_SALES.md, clicking the dashboard's own buttons in a real
// browser. Run it against a server with simulated payments (the hosted page is the simulated one):
//
//   PAYMENTS=simulated SELLER_MODE=rules GS_VAR=var/e2e PYTHONPATH=src python scripts/group_sales.py &
//   node scripts/e2e_demo.js                                  # BASE defaults to http://localhost:8010
//   BASE=https://harbour-house-preview.onrender.com TOKEN=... node scripts/e2e_demo.js
//
// Needs Playwright for Node (npm i playwright; npx playwright install chromium). It resets the
// scenario first, so don't point it at a demo someone is presenting. The buyer's moves are made in
// the buyer's mailbox page (/inbox/<ref>), as in the demo; the hotel's in the dashboard. Exit code 1 if any check fails.
const { chromium } = require('playwright');

const BASE = (process.env.BASE || 'http://localhost:8010').replace(/\/$/, '');
const TOKEN = process.env.TOKEN || '';
const SHOTS = process.env.SHOTS || '';            // a folder to save a screenshot per step, if set
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok || !detail ? '' : '  — ' + detail}`); };

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [], dialogs = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => { dialogs.push(d.message()); d.dismiss(); });

  const state = () => page.evaluate(() => fetch('/api/state').then(r => r.json()));
  const deal = async name => (await state()).deals.find(d => d.company.startsWith(name));
  // Wait until the seller agent is idle and the deal is in one of the given states.
  const settle = async (name, statuses, ms = 30000) => {
    const t0 = Date.now(); let d;
    while (Date.now() - t0 < ms) {
      const s = await state(); d = s.deals.find(x => x.company.startsWith(name));
      if (!s.runner.busy && (!statuses || statuses.includes(d.status))) { await page.waitForTimeout(700); return d; }
      await page.waitForTimeout(300);
    }
    throw new Error(`${name} stuck in ${d && d.status}, expected ${statuses}`);
  };
  const open = async name => { await page.click(`#queue >> text=${name}`); await page.waitForTimeout(400); };
  const tab = async key => { await page.click(`[data-tab=${key}]`); await page.waitForTimeout(300); };
  const card = () => page.innerText('#deal');
  const click = async (sel) => { await page.click(sel); await page.waitForTimeout(500); };
  const demo = async on => {                                         // open or close Demo controls
    if ((await page.$eval('#demo', el => el.classList.contains('open'))) !== on) await click('#demoBtn');
  };
  const shot = async n => SHOTS && page.screenshot({ path: `${SHOTS}/${n}.png`, fullPage: true });

  // The buyer side lives in each buyer's mailbox page, outside the hotel dashboard.
  const mailbox = async name => {
    const d = await deal(name);
    const m = await ctx.newPage();
    m.on('pageerror', e => errors.push('mailbox: ' + e.message));
    m.on('dialog', x => { dialogs.push(x.message()); x.dismiss(); });
    await m.goto(BASE + d.inbox_url); await m.waitForSelector('#act .box, #act p', { timeout: 15000 });
    return m;
  };
  const mclick = async (m, sel) => { await m.click(sel); await m.waitForTimeout(700); };

  try {
    await page.goto(`${BASE}/${TOKEN ? '?k=' + encodeURIComponent(TOKEN) : ''}`);
    await page.waitForSelector('#queue .q', { timeout: 60000 });   // a sleeping free server can take a minute

    // 1. Reset: the seller starts on its own. Northstar gets questions, Summit a quote.
    await demo(true); await click('#reset'); await click('#resetYes'); await demo(false);
    check('reset asks inline, no browser dialog', dialogs.length === 0);
    let n = await settle('Northstar', ['needs_info']);
    const s1 = await settle('Summit', ['offer_sent']);
    check('the seller started by itself after the reset', !!n && s1.status === 'offer_sent');
    check('no Run seller button anywhere', (await page.$$('[data-act=run], #runSeller')).length === 0);
    check('Summit quoted £4,920 with a £1,476 deposit', s1.versions[0].pricing.total === 492000 && s1.versions[0].pricing.deposit === 147600);
    check('Northstar asked three questions', (n.thread.find(m => m.questions) || {}).questions?.length === 3);
    await open('Northstar');
    check('the dashboard has no buyer actions', (await page.$$('#deal [data-act=sendSug], #deal [data-act=accept], #deal [data-form]')).length === 0);
    check('the card shows the seller\'s last action', /Seller's last action/.test(await page.innerText('#deal .next')));
    await tab('agent');
    let txt = await card();
    check('AI Agent tab lists Northstar actions', /Recorded the requirements/.test(txt) && /Asked the buyer/.test(txt), txt.slice(0, 200));
    check('AI Agent tab shows only this deal', !/Summit/.test(txt));
    await page.click('details.ag'); await page.waitForTimeout(2500);   // a refresh must not close it
    check('an opened AI Agent action stays open across refreshes', await page.$$eval('details.ag[open]', x => x.length) === 1);
    await open('Summit'); await tab('agent');
    check('Summit AI Agent tab shows its offer', /Proposed an offer/.test(await card()));
    await shot('1-quotes');

    // 2-3. In Northstar's mailbox: answers, then the counteroffer. v2 at £3,840 waits for the manager.
    const mb = await mailbox('Northstar');
    check('the mailbox shows the hotel\'s questions', /Before we can confirm/.test(await mb.innerText('#thread')));
    await mclick(mb, '[data-act=sendSug]');                         // Send answers
    n = await settle('Northstar', ['offer_sent']);
    const v1 = n.versions[n.versions.length - 1];
    check('v1 £9,600, 30% deposit, 10% attrition', v1.pricing.total === 960000 && v1.terms.deposit_pct === 30 && v1.terms.attrition_pct === 10);
    await mb.waitForSelector('.att', { timeout: 10000 });
    check('the offer arrives in the mailbox with its figures', /£9,600/.test(await mb.innerText('#thread')));
    await mclick(mb, '[data-act=sendSug]');                         // Send counteroffer
    n = await settle('Northstar', ['pending_approval']);
    const v2 = n.versions[n.versions.length - 1];
    check('v2 keeps £220 and asks 40% (£3,840)', v2.terms.rate === 22000 && v2.pricing.deposit === 384000);
    await open('Northstar');
    await click('#deal [data-ap][data-d=approve]');                  // hotel manager, on the dashboard
    n = await settle('Northstar', ['offer_sent']);

    // 4. Accept v2 in the mailbox: over Northstar's limits, so its organizer signs off there too.
    await mb.waitForSelector('button:has-text("Accept v2")', { timeout: 10000 });
    await mclick(mb, 'button:has-text("Accept v2")');
    n = await settle('Northstar', ['acceptance_pending_organizer']);
    const accMsg = n.thread.filter(m => m.from === 'buyer').pop().text;
    check('the accept message states no limits', !/£3,000|30% limit|budget/.test(accMsg), accMsg);
    check('the dashboard only says the buyer is signing off', /organizer is signing off/.test(await page.innerText('#deal .next'))
          && (await page.$$('#deal [data-ap]')).length === 0);
    await mb.waitForSelector('[data-org][data-d=approve]', { timeout: 10000 });
    await mclick(mb, '[data-org][data-d=approve]');                  // organizer, in the mailbox
    n = await settle('Northstar', ['awaiting_payment']);

    // 5. Pay from the mailbox on the (simulated) hosted page; returning confirms the booking.
    await mb.waitForSelector('button:has-text("Open payment page")', { timeout: 10000 });
    const [pay] = await Promise.all([ctx.waitForEvent('page'), mb.click('button:has-text("Open payment page")')]);
    await pay.waitForLoadState();
    await pay.click('text=Pay deposit'); await pay.waitForLoadState();
    await pay.click('button:has-text("Pay")'); await pay.waitForLoadState();
    await pay.waitForTimeout(800);
    check('return page shows the booking confirmed', /confirmed/i.test(await pay.innerText('body')));
    await pay.close();
    n = await settle('Northstar', ['confirmed']);
    check('booking confirmed after the verified £3,840 payment', n.booking && n.booking.status === 'confirmed' && n.finance_view.received === 384000);

    // 6. Jump to 20 Jan: Summit lapses; Northstar reduces to v3 £8,280 with £528 owed.
    await page.waitForTimeout(500);
    await open('Northstar');
    await click('#deal [data-act=jump]');
    await settle('Summit', ['expired']);
    check('Summit lapsed at the jump', (await deal('Summit')).status === 'expired');
    await mb.reload(); await mb.waitForSelector('[data-act=sendSug]', { timeout: 10000 });
    await mclick(mb, '[data-act=sendSug]');                          // Send change request
    n = await settle('Northstar', ['amendment_sent']);
    await mb.waitForSelector('button:has-text("Accept v3")', { timeout: 10000 });
    await mclick(mb, 'button:has-text("Accept v3")');
    n = await settle('Northstar', ['refund_awaiting_approval']);
    const v3 = n.versions[n.versions.length - 1];
    check('v3 £8,280 with £528 owed', v3.pricing.total === 828000 && n.finance.refund_owed === 52800);

    // 7. Approve the £528 refund; the seller creates it and checks it; the ledger updates once.
    await open('Northstar');
    await click('#deal [data-ap][data-d=approve]');
    const t0 = Date.now();
    while (Date.now() - t0 < 30000) {                                 // the refund settles; "Check now" if waiting
      n = await deal('Northstar');
      if (n.status === 'confirmed') break;
      const btn = await page.$('#deal [data-act=chkRef]');
      if (btn) await btn.click();
      await page.waitForTimeout(1500);
    }
    n = await settle('Northstar', ['confirmed']);
    let s = await state();
    const refunds = s.operations.filter(o => o.deal_id === n.id && o.kind === 'refund');
    check('exactly one £528 refund, completed', refunds.length === 1 && refunds[0].amount === 52800 && refunds[0].status === 'completed');

    // 8. Deliver the held notification: a duplicate, nothing changes. Final figures.
    const ledgerBefore = s.ledger.length;
    await demo(true); await click('#releaseN'); await demo(false);
    s = await state(); n = s.deals.find(d => d.company.startsWith('Northstar'));
    check('late notification changes nothing', s.ledger.length === ledgerBefore);
    check('£3,312 net received, £4,968 still due', n.finance_view.net_received === 331200 && n.finance_view.still_due === 496800,
          JSON.stringify(n.finance_view));
    check('10 twins a night', Object.values(n.booking.rooms).every(r => r === 10));
    await mb.reload(); await mb.waitForSelector('.box.ok', { timeout: 10000 });
    check('the mailbox shows the booking and £3,312 paid', /£3,312 paid/.test(await mb.innerText('#act')));
    await tab('money');
    txt = await card();
    check('Payments tab shows £3,312 and £4,968', /£3,312/.test(txt) && /£4,968/.test(txt));
    await tab('agent');
    check('AI Agent tab shows the refund started and checked', /Started a refund/.test(await card()) && /Checked the refund/.test(await card()));
    await shot('3-final');

    // Seller rules page: lists the rules, refuses an inconsistent change, saves and restores one.
    await click('#rulesBtn'); await page.waitForSelector('#rules table', { timeout: 10000 });
    check('Seller rules page opens from the left column', await page.isVisible('#rules') && !(await page.isVisible('#desk')));
    txt = await page.innerText('#rules');
    check('rules page shows who decides, with the current figures', /who decides/i.test(txt) && /£210 or more/.test(txt) && /£195 to £209/.test(txt));
    await page.fill('[data-path="rms.rates.twin.autonomous_floor"]', '230'); await click('#rulesSave');
    check('an inconsistent rule is refused with a reason', /not saved/i.test(await page.innerText('#rules')) && (await state()).activity.every(a => a.kind !== 'rules_changed'));
    await click('#rulesUndo');
    await page.fill('[data-path="policy.seller_refund_limit"]', '600'); await click('#rulesSave'); await page.waitForTimeout(500);
    txt = await page.innerText('#rules');
    check('a valid change is saved and shown', /changed · default £500/.test(txt) && /up to £600 in total/.test(txt));
    await click('#rulesDefault'); await page.waitForTimeout(500);
    check('restore defaults puts the limit back', /up to £500 in total/.test(await page.innerText('#rules')) && !(await page.$('#rulesDefault')));
    await shot('4-rules');
    await click('#rulesBack');

    // 9. Availability button under the inquiries, and back.
    await click('#availBtn');
    check('Availability page opens from the left column', await page.isVisible('#avail') && !(await page.isVisible('#desk')));
    check('availability opens on the demo stay month', (await page.innerText('#availMonth')) === 'March 2027'
      && /10 Mar night/.test(await page.innerText('#availDay')));
    check('the year strip offers every month of the bookable window', (await page.$$('#availMonths button')).length === 12);
    await click('#availMonths [data-month="2027-06"]');
    await click('#availCal [data-day="2027-06-15"]');
    check('picking June and a night shows that night', (await page.innerText('#availMonth')) === 'June 2027'
      && /15 Jun night/.test(await page.innerText('#availDay')) && /\d+ free of 20/.test(await page.innerText('#availDay')));
    await click('#avail [data-step="-1"]');
    check('previous month goes back to May', (await page.innerText('#availMonth')) === 'May 2027');
    await page.waitForTimeout(2500);                       // the dashboard re-renders from fresh data meanwhile
    check('the chosen month survives a refresh of the data', (await page.innerText('#availMonth')) === 'May 2027');
    await shot('5-availability');
    await click('#availBack');
    check('Back to deals returns', await page.isVisible('#desk'));

    // 10. Phone width: no sideways scroll on any tab of either deal.
    await page.setViewportSize({ width: 375, height: 800 });
    let wide = [];
    for (const name of ['Northstar', 'Summit']) {
      await open(name);
      for (const k of ['conv', 'agent', 'act', 'req', 'rec', 'hist', 'money', 'ops']) {
        await tab(k);
        if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) wide.push(`${name}/${k}`);
      }
    }
    await click('#rulesBtn');
    if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) wide.push('rules');
    await click('#rulesBack');
    await mb.setViewportSize({ width: 375, height: 800 }); await mb.waitForTimeout(300);
    if (await mb.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) wide.push('mailbox');
    check('no horizontal scroll at 375px', wide.length === 0, wide.join(', '));
  } catch (e) {
    check('run completed', false, e.message);
    await shot('error');
  }
  check('no JavaScript errors', errors.length === 0, errors.join(' | '));
  check('no browser dialogs', dialogs.length === 0, dialogs.join(' | '));
  await browser.close();
  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
})();
