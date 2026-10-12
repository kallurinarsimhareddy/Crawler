# Cost analysis

**Exchange rate:** **₹88 = $1** is an assumption; change it as needed.

**Price sources** were checked on 2026-10-09 and are listed at the end. Anything marked **UNKNOWN** needs the account owner to look it up. No prices were invented. Hardware the user already owns is kept separate and counted as $0 recurring.

## 1. What SANA GTM pays for today

| Service | Used for | Plan | Monthly cost | Status at runtime |
|---|---|---|---|---|
| Supabase | PostgreSQL (452 MB) + Auth (6 users) | **UNKNOWN**: not visible from this machine. Free = 500 MB DB cap, $0; Pro = $25/mo with 8 GB DB | $0 or $25 (₹0 or ₹2,200) | **Required** today (DB + Auth) |
| Upstash Redis | Former task queue | Free tier (500K commands/mo), exhausted on 2026-10-04 | $0 | **Not used** since the 2026-10-06 PostgreSQL-queue cutover; the variables remain in the env files |
| Cloudflare Pages | Frontend (`sanagtm`) | Free | $0 | Required (or any static host) |
| Cloudflare quick tunnel | API exposure | Free, no account; URL changes on every restart | $0 | Required today; fragile |
| GitHub | Code (public repo) | Free | $0 | Development only |
| Gemini API | AI features in free-only mode | Free tier key; workspace guard holds spend at $0 | $0 | Optional |
| Anthropic Claude API | AI features | Key present; last provider test returned 401 (the key was rejected) | $0 now; pay per token if fixed | Optional |
| EmailListVerify | "VALID" verdicts for email validation | Prepaid credits: balance 8,389 on 2026-10-01; plan and top-up price **UNKNOWN** | $0 recurring (prepaid) | Optional; without it, results are VALID-less (INVALID / NOT VERIFIED only) |
| Seamless.AI | Contact enrichment | Account verified; balance 0 credits; subscription price **UNKNOWN** | UNKNOWN | Optional |
| ZoomInfo | Company/contact search and enrich | Existing RiseITS subscription; API client credentials not set on this machine | UNKNOWN (business contract, not SANA-specific) | Optional |
| USAJOBS / Adzuna / ZipRecruiter | Job sources | Not configured (USAJOBS and Adzuna have free developer keys; ZipRecruiter is a partner program) | $0 | Optional |
| SMTP / Gmail / MS Graph / Postmark / SendGrid | Invites and outbound sequences | Not configured; sending is hard-gated off | $0 | Optional |
| Electricity + internet | This PC already runs SANA GTM 24/7 | Existing | see §3 | Required |

**Today's paid-service total** is $0 if Supabase is on Free, or $25/month (₹2,200) if it is on Pro. Usage-based AI, ELV, Seamless and ZoomInfo are extra.

## 2. Self-hosted configuration costs

### Recurring

| Item | Option A (minimal-risk, keep Supabase Auth) | Option B (fully self-hosted) |
|---|---|---|
| PostgreSQL, API, worker, crawlers, backups | VM on owned hardware: **$0** | **$0** |
| Supabase | Auth only on the Free plan: **$0** (the published Free Auth limit is 50,000 MAU; verify. The DB is no longer used, so the 500 MB cap does not matter). **Caveat:** Supabase pauses inactive Free projects, so a low-traffic auth-only project could pause; verify the current policy. This is one reason to finish Option B. | **$0** (removed) |
| Upstash | **$0** (cancel or leave idle; a pay-as-you-go database idles at $0) | **$0** |
| Frontend | Cloudflare Pages Free: **$0** | **$0** (Pages or served by the VM) |
| Stable API hostname | Named Cloudflare Tunnel: **$0**. Needs a domain whose DNS zone is on Cloudflare. A subdomain of a domain you already own costs $0 extra. A new `.com` is about $10–11/year, **UNVERIFIED**; check Cloudflare Registrar's at-cost price | same |
| TLS certificates | Cloudflare edge: **$0** | **$0** |
| Off-host backup copy | Second internal or USB disk you own: $0 recurring. Optional cloud object storage: price **UNKNOWN**; a few GB is a small amount at mainstream providers | same |
| Electricity (incremental) | §3 | §3 |
| **Total paid services** | **$0/month** + optional usage APIs | **$0/month** + optional usage APIs |

### One-time

| Item | Cost |
|---|---|
| VMware Workstation Pro (free for personal and commercial use since 2024) | $0. Confirm the licence terms at download time. |
| Ubuntu Server 24.04 LTS | $0 |
| Domain, only if no existing domain can be used | about $10–11/year, UNVERIFIED |
| Extra disk for off-host backups, only if not already owned | UNKNOWN (market price) |
| Engineering time to migrate | Phase 1 is about 1–2 working days; the auth replacement (Option B) is about 3–5 days plus testing |

## 3. Electricity and internet

**Hardware.** The host PC (Intel i3-12100, 4 cores / 8 threads, 32 GB RAM, one 512 GB NVMe) **already runs 24/7** for the current SANA GTM setup.

**Power draw (estimated; not measured; use a plug-in meter to confirm).** A desktop of this class draws roughly 30–50 W at idle or light load. Running the workload in a VM instead of directly on Windows adds very little: about 2–5 W while workers run, plus the hypervisor's own RAM.

| Scenario | kWh / month | At ₹7.65/unit | At ₹9.50/unit |
|---|---|---|---|
| Whole PC on 24/7 at 40 W (if it were not already on) | 29 | ₹223 (~$2.5) | ₹277 (~$3.2) |
| Incremental for the VM on an already-on PC (+5 W) | 3.7 | ₹28 | ₹35 |

**Tariff source.** Telangana TSSPDCL domestic slabs (FY 2026-27) reach ₹7.65/unit (401–800 units) and ₹9.50/unit (above 800 units), plus 5% duty and a ₹120 fixed charge.

**UNKNOWN:**
- Which tariff the office pays. A commercial (LT-II) tariff is usually higher.
- Whether the PC is billed domestic or commercial.
- The real wattage.

**Internet (estimated).**

| Traffic | Estimate |
|---|---|
| WAD daily incremental | about 100–150 page requests |
| WAD full sweep | about 10,100 requests; at an assumed 50–100 KB each, about 0.5–1 GB per sweep |
| Weekly sweeps + dailies + UI | about **3–6 GB per month** |

This is negligible on an unlimited broadband plan. Upload bandwidth limits large CSV uploads through the tunnel: 2.1 MB took about 40 s on 2026-09-30.

## 4. Zero-paid-service configuration

**Technically feasible today** with Option A, using the free tiers of Supabase Auth, Cloudflare Pages and Cloudflare Tunnel. With Option B, nothing external is required except Cloudflare.

**Features that degrade when paid services are absent:**

| Removed | What is lost | What still works |
|---|---|---|
| AI APIs (Claude/OpenAI) | LLM scraper planning, research summaries, agent chat, AI scoring | Deterministic extractor, rules-based scoring, `RulesProvider`; Gemini free tier while its quota lasts |
| EmailListVerify | "VALID" verdicts | INVALID / NOT VERIFIED from format, DNS, MX, SPF and DMARC checks |
| Seamless.AI / ZoomInfo | Contact discovery and enrichment | Manual CRM, CSV import, Account Builder from job data |
| SMTP provider | Emailed invites, sequences | Copy-link invites (as today) |
| Partner job feeds (LinkedIn/Indeed/ZipRecruiter) | Those sources | WAD, 60 ATS adapters, public ATS APIs, USAJOBS/Adzuna (free keys) |

## 5. Cost summary

| | Today | Option A | Option B |
|---|---|---|---|
| Paid infrastructure per month | $0 (Supabase Free) or $25 / ₹2,200 (Pro): plan **UNKNOWN** | **$0** | **$0** |
| Electricity (the PC is already on 24/7) | ₹223–277 for the whole PC | +₹28–35 incremental | same as A |
| Domain | $0 (quick tunnel) | $0 with an existing domain, else about $10–11/year | same |
| Usage-based optional APIs | unchanged | unchanged | unchanged |
| Database growth headroom | 500 MB cap (Free) or 8 GB (Pro) | limited only by local disk | same |

**Information needed to finish the numbers:**
1. The Supabase plan for project `<supabase-project-ref>`.
2. EmailListVerify, Seamless and ZoomInfo contract prices.
3. Which domain to use for the API hostname, and which Cloudflare account holds its zone.
4. The electricity tariff category and measured wattage.

## Sources

- Supabase plans: <https://supabase.com/pricing>; secondary summaries <https://selfhost.dev/supabase-pricing-explained/>, <https://schematichq.com/blog/supabase-pricing>
- Upstash Redis pricing: <https://upstash.com/pricing/redis>, <https://upstash.com/docs/Redis/overall/pricing>
- Telangana tariffs FY 2026-27: <https://desiutility.com/electricity/tariffs/tsspdcl>, <https://solarquarter.com/2026/04/13/telangana-electricity-regulatory-commission-tariff-order-fy27-balancing-discom-finances-consumer-tariffs-and-power-supply-stability/>
