// Economy page data — snapshots are the fallback when the live FRED fetch
// is blocked (fred.stlouisfed.org does not send CORS headers). Snapshot date:
// 2026-10-30. "whyNow" notes are dated research, refreshed by the research pass.
window.ECONOMY_SNAPSHOT_DATE = '2026-10-30';

window.ECONOMY_INDICATORS = [
  {
    id: 'fed_funds', fredId: 'DFF', kind: 'rate',
    name: 'Federal Funds Rate',
    unit: '%', decimals: 2,
    value: 3.88, asOf: '2026-10-28',
    ref30: 3.88, ref90: 3.63,
    whatIs: 'The interest rate banks charge each other overnight. The Fed sets a target range for it, and it is the master dial — almost every other rate in the economy takes its cue from this one.',
    meansForYou: 'When this falls, borrowing gets cheaper across the board: credit cards, auto loans, and eventually mortgages. When it rises, the opposite. Your 3% mortgage is locked and doesn\'t care — but any land loan or business credit you take out does.',
    whyNow: 'The Fed under Chair Kevin Warsh has pivoted from easing back to tightening. The September hike — the first since 2023 — came because inflation re-accelerated (CPI hit 3.7% in September, core rose to 2.6%) and Q3 GDP beat forecasts on AI/data-center spending. The October hold was a \'hawkish skip\': the statement upgraded growth language, and Warsh said the Fed will \'deliver price stability, no ifs ands or buts.\' Even the September jobs report (weak +29k) only bought a one-meeting pause; two dissents wanted higher rates NOW.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'Federal Reserve — monetary policy', url: 'https://www.federalreserve.gov/monetarypolicy.htm' },
      { label: 'FRED: Effective Federal Funds Rate', url: 'https://fred.stlouisfed.org/series/DFF' }
    ]
  },
  {
    id: 'mortgage30', fredId: 'MORTGAGE30US', kind: 'rate',
    name: '30-Year Mortgage Rate',
    unit: '%', decimals: 2,
    value: 7.24, asOf: '2026-10-29',
    ref30: 7.28, ref90: 6.66,
    whatIs: 'The average US rate on a 30-year fixed mortgage, surveyed weekly by Freddie Mac. It follows the 10-year Treasury yield more than the Fed — lenders price long loans off long bonds.',
    meansForYou: 'This is the number that decides what a land buy or a future home costs you per month. At 7%+, every $100K borrowed costs roughly $665/month. If you are scouting land, this rate is your filter: it has to work at today\'s rate, not at a hoped-for lower one.',
    whyNow: 'Mortgages track the 10-year Treasury yield, which fell from its 5.31% October 5 peak to ~4.99% after the Fed held rates and September PCE inflation came in slightly cooler than expected. But the relief is small — rates are up ~1.3 percentage points from the February 2026 low of 5.98%, with nearly half that climb coming since early September. Freddie Mac\'s Sam Khater calls it only \'modestly boosting purchasing power.\' Fannie Mae\'s October forecast keeps rates above 7% through all of 2027.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'Freddie Mac PMMS (weekly survey)', url: 'https://www.freddiemac.com/pmms' },
      { label: 'FRED: 30-Yr Fixed Rate Mortgage Average', url: 'https://fred.stlouisfed.org/series/MORTGAGE30US' }
    ]
  },
  {
    id: 'treasury10', fredId: 'DGS10', kind: 'rate',
    name: '10-Year Treasury Yield',
    unit: '%', decimals: 2,
    value: 5.05, asOf: '2026-10-28',
    ref30: 5.25, ref90: 4.61,
    whatIs: 'What the US government pays to borrow for 10 years. Think of it as the economy\'s gravity — mortgage rates, corporate borrowing, and the "safe" return your money could earn instead of risking it all key off this number.',
    meansForYou: 'When the 10-year is high, safe money pays well and risky bets have to clear a higher bar. It is also the best single read on where markets think inflation and growth are heading.',
    whyNow: 'The October peak came from the one-two punch of the Fed\'s September rate HIKE and hot September CPI (3.7%), with futures pricing more hikes in December and March. It eased off the peak because cooler September PCE data and the Fed\'s \'hawkish skip\' in October made investors feel the worst of the repricing was done. Underneath, the driver since late February has been the US-Iran conflict: oil above $115/barrel, record diesel prices, and steel/aluminum tariff costs are feeding straight into inflation expectations that the bond market has to price in. Last close above 5% before this year: July 2007.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'FRED: 10-Year Treasury Constant Maturity', url: 'https://fred.stlouisfed.org/series/DGS10' },
      { label: 'St. Louis Fed — explaining yields', url: 'https://www.stlouisfed.org/' }
    ]
  },
  {
    id: 'cpi', fredId: 'CPIAUCSL', kind: 'yoy',
    name: 'Inflation (CPI, year over year)',
    unit: '%', decimals: 1,
    value: 3.7, asOf: '2026-09-01',
    ref30: 3.7, ref90: 3.6,
    whatIs: 'How much more the same basket of everyday goods costs than a year ago. The Fed\'s target is 2% — anything above that is the reason your dollar buys less each year.',
    meansForYou: 'Inflation is the silent partner in every plan: it pushes your costs up, but it also shrinks the real weight of fixed debts (like a 3% mortgage — inflation is quietly paying part of it for you). For NicNacs, it sets how fast resale prices and the wage floor move.',
    whyNow: 'This is an energy shock, not a demand boom. The US-Iran conflict that began in late February 2026 drove Brent crude above $115/barrel and the average US gas price to $4.29/gallon (~43% above pre-war). That one gasoline surge did most of the September damage — but core inflation re-accelerating from 2.4% to 2.6% is what spooked the Fed, because it shows price pressure spreading beyond fuel. September PCE (the Fed\'s preferred gauge) came in slightly cooler than expected, which is why the Fed paused in October. CAUTION: the October CPI (due Nov 10) won\'t have clean year-over-year comparisons — BLS missed October 2025 data during a federal lapse, so the next YoY print will be distorted.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'BLS — Consumer Price Index', url: 'https://www.bls.gov/cpi/' },
      { label: 'FRED: CPI for All Urban Consumers', url: 'https://fred.stlouisfed.org/series/CPIAUCSL' }
    ]
  },
  {
    id: 'unemployment', fredId: 'UNRATE', kind: 'rate',
    name: 'Unemployment Rate',
    unit: '%', decimals: 1,
    value: 4.2, asOf: '2026-09-01',
    ref30: 4.1, ref90: 4.0,
    whatIs: 'The share of people who want work and can\'t find it. Low means workers have bargaining power and wages rise; rising means the job market is loosening and a slowdown may be brewing.',
    meansForYou: 'For NicNacs hiring (the grant assumes December hiring): a looser labor market means easier hiring at the wage floor. For the economy read: a fast-rising unemployment rate is one of the classic recession flags.',
    whyNow: 'Economists call this \'low-hire, low-fire\': companies are keeping the workers they have (layoffs are near 57-year lows) but barely adding new ones. Hiring averaged just ~51k/month over the last three months. The unemployment tick-up is mostly a rounding artifact (4.14→4.18%) driven by more people RE-ENTERING the labor force (participation up, household employment up 406k), not mass layoffs. Analysts cite stricter immigration enforcement shrinking the labor force and firms too nervous about the war/supply chains to expand payrolls. The bright side: wage growth cooling to 3.0% helps on inflation — but it did NOT keep up with prices in July or August.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'BLS — Employment Situation', url: 'https://www.bls.gov/news.release/empsit.nr0.htm' },
      { label: 'FRED: Unemployment Rate', url: 'https://fred.stlouisfed.org/series/UNRATE' }
    ]
  },
  {
    id: 'yield_curve', fredId: 'T10Y2Y', kind: 'spread',
    name: 'Yield Curve (10Y − 2Y)',
    unit: 'pp', decimals: 2,
    value: 0.37, asOf: '2026-10-29',
    ref30: 0.34, ref90: 0.40,
    turning: true,
    whatIs: 'The gap between long-term and short-term government borrowing costs. When it goes negative ("inverted"), markets are betting on trouble ahead — it has predicted nearly every US recession. When it is positive and steady, no alarm.',
    meansForYou: 'This is the early-warning radar. You do not trade off it — you watch it. A curve flipping negative is the "pay attention" moment, usually 6–18 months before a downturn shows up in jobs.',
    whyNow: 'This is the \'turning signal\' — and it is NOT flashing recession. When short-term rates rise above long-term rates (an \'inverted\' curve), it usually means a downturn is coming; that inversion cleared in September 2024 and the curve has stayed normal since. What\'s notable now is the opposite dynamic: BOTH ends are high and the long end led the October selloff, because the bond market believes the Fed will have to hike again (December and March are priced in) and that inflation stays sticky for years. A positive-but-flat curve like this says \'hot economy, tight policy\' — growth continuing under pressure, not a looming contraction.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'FRED: 10Y-2Y Treasury Spread', url: 'https://fred.stlouisfed.org/series/T10Y2Y' }
    ]
  },
  {
    id: 'jobless', fredId: 'ICSA', kind: 'count',
    name: 'New Jobless Claims',
    unit: '', decimals: 0,
    value: 194000, asOf: '2026-10-24',
    ref30: 199000, ref90: 205000,
    turning: true,
    format: 'thousands',
    whatIs: 'How many people filed for unemployment benefits in a week. It is the fastest real-time read on layoffs — no waiting for the monthly jobs report.',
    meansForYou: 'Falling claims = companies are holding onto workers = the job market is still solid. A sharp sustained rise is the first place a downturn shows up, weeks before the headlines.',
    whyNow: 'Layoffs are extraordinarily rare — claims haven\'t topped 230,000 in any week all year, and the mid-July print of 189,000 was the lowest since 1969 (the 20-year pre-pandemic average was ~345,000). Employers are hoarding workers because replacing them is expensive and the labor pool is tight (immigration enforcement + retirements are shrinking it). The catch: LOW layoffs coexist with WEAK hiring — continuing claims at 1.7M show that people who DO lose jobs take a long time to find new ones. This paradox (safe jobs, hard searches) is exactly why the Fed can\'t cut: workers still have income and spending power, which keeps inflation pressure alive.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'DOL — Unemployment Insurance Weekly Claims', url: 'https://www.dol.gov/ui/data.pdf' },
      { label: 'FRED: Initial Claims', url: 'https://fred.stlouisfed.org/series/ICSA' }
    ]
  },
  {
    id: 'sentiment', fredId: 'UMCSENT', kind: 'index',
    name: 'Consumer Sentiment',
    unit: '', decimals: 1,
    value: 48.1, asOf: '2026-09-01',
    ref30: 51.7, ref90: 55.0,
    turning: true,
    whatIs: 'A monthly survey of how regular people feel about their finances and the economy. It is not the economy itself — it is the mood — but mood drives spending, and spending is 70% of the economy.',
    meansForYou: 'For a thrift store, sentiment cuts both ways: gloomy shoppers trade down to thrift (good for sales), but they also spend less overall. For land and big buys: weak sentiment often means motivated sellers.',
    whyNow: 'This is the gap between a strong economy and angry consumers. People feel squeezed by prices — gasoline was mentioned unprompted by over 40% of respondents, and the share saying high prices are hurting their finances rose a fourth straight month to 56%. Durable-goods buying conditions plunged 18% on higher rates. Sentiment is 15% below February, right before the Iran conflict, and the pain is real for lower-wealth households (stock-owners report little change in living standards). The odd silver lining: expectations ticked up slightly (48.8 vs 46.3) — consumers see the current pain but expect the year ahead to be a bit less bad. Released one week before the midterms, this is why inflation is voters\' top issue.',
    whyNowDate: '2026-10-30',
    links: [
      { label: 'Surveys of Consumers — Univ. of Michigan', url: 'http://www.sca.isr.umich.edu/' },
      { label: 'FRED: Consumer Sentiment', url: 'https://fred.stlouisfed.org/series/UMCSENT' }
    ]
  }
];

// Regime read — replaced by the research pass with a dated assessment.
window.ECONOMY_REGIME = {
  text: 'The economy is NOT turning toward recession — it is turning toward **renewed tightening and a hotter-for-longer regime**. The single biggest signal is the Fed itself: after three years without a hike, it raised rates in September and two of ten voters wanted another hike in October, with futures pricing a December hike at ~82%. Second, inflation is re-accelerating on an energy shock — the US-Iran conflict (late Feb 2026) pushed oil above $115/barrel and gasoline 34% higher year-over-year, and core CPI ticking up to 2.6% shows it is spreading past fuel. Third, the labor market is sending a "no recession" signal — layoffs at 57-year lows, claims at 194k, the yield curve positively sloped — while being too weak to reassure anyone (hiring at ~51k/month, sentiment at near-record lows). The honest read: growth is holding (Q3 GDP beat, manufacturing expanding a ninth straight month, AI/data-center capex booming) but inflation is the tax everyone is paying, and the Fed is choosing to fight prices rather than protect growth.',
  date: '2026-10-30'
};
