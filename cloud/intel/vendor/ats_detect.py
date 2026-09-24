# VENDORED from C:/Users/Narsimha Reddy/CareerAutomation/ats.py (not a git repo) on 2026-09-24 (pure logic, no network). Keep behaviour identical to the original.
"""ATS registry: detection, canonicalization, and job-harvest endpoints.

Extensible: add a tuple to RULES to support a new ATS platform.
"""
import re
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qs

LOCALE_RE = re.compile(r'^[a-z]{2}([-_][A-Za-z]{2})?$')
URL_RE = re.compile(r'https?://[^\s"\'<>()\\\[\]{}|^`]+', re.I)

# Never accept these as "the company's official ATS"
AGGREGATORS = {
    'indeed.com', 'linkedin.com', 'glassdoor.com', 'ziprecruiter.com', 'monster.com',
    'simplyhired.com', 'careerbuilder.com', 'jooble.org', 'talent.com', 'adzuna.com',
    'builtin.com', 'dice.com', 'wellfound.com', 'angel.co', 'snagajob.com', 'jobcase.com',
    'glassdoor.co.uk', 'neuvoo.com', 'lensa.com', 'jobs2careers.com', 'upwork.com',
    'flexjobs.com', 'themuse.com', 'levels.fyi', 'salary.com', 'jobtarget.com',
    'recruit.net', 'trovit.com', 'careerjet.com', 'learn4good.com', 'joblist.com',
    'resume-library.com', 'nexxt.com', 'startwire.com', 'jobrapido.com', 'whatjobs.com',
    'jobsradar.com', 'jobvertise.com', 'usajobs.gov', 'craigslist.org', 'facebook.com',
    'twitter.com', 'x.com', 'youtube.com', 'instagram.com', 'crunchbase.com', 'zoominfo.com',
    'bloomberg.com', 'wikipedia.org', 'bing.com', 'google.com', 'duckduckgo.com',
    'yahoo.com', 'reddit.com', 'quora.com', 'jobsearcher.com', 'myjobhelper.com',
    'jobted.com', 'jobilize.com', 'higheredjobs.com', 'idealist.org', 'ihire.com',
    'clearancejobs.com', 'jobgether.com', 'himalayas.app', 'remoterocketship.com',
    'getwork.com', 'diversityjobs.com', 'localjobnetwork.com', 'jobs.net', 'employmentcrossing.com',
}

# Static assets / non-board paths that must never be saved
BAD_PATH_RE = re.compile(
    r'(\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|pdf|xml|json|txt|zip|mp4|webp)$)'
    r'|/(static|assets|cdn|dist|bundle|_next|__|media|images|img|fonts)/'
    r'|messagebundle|pendo\.io|/pendo/|googletagmanager|google-analytics|/gtm\.|hotjar|'
    r'cloudfront\.net/|akamaihd|/favicon', re.I)

LOGIN_RE = re.compile(r'/(login|signin|sign-in|auth|sso|logout|register|account/|password|'
                      r'candidate/login|myaccount|profile/login)', re.I)

# Query params to always strip (tracking / filters / pagination / session)
STRIP_PARAMS = re.compile(
    r'^(utm_|_hs|hs[a-z]*|fbclid|gclid|msclkid|mc_[a-z]+|ref|referer|referrer|source|src|'
    r'campaign|medium|gh_src|gh_jid|lever-source|trackid|trk|sessionid|jsessionid|phpsessid|'
    r'page|pagenum|p|start|offset|limit|from|sort|sortby|order|q|query|search|searchtext|'
    r'keyword|keywords|location|loc|city|state|country|region|dept|department|category|cat|'
    r'team|function|jobtype|type|filter|filters|facet|lang|locale|hl|tab|view|mode)$', re.I)


def _segs(path):
    return [s for s in (path or '').split('/') if s]


def _build(host, segs=None, query=None):
    p = '/' + '/'.join(segs) if segs else '/'
    q = urlencode(query, doseq=True) if query else ''
    return urlunsplit(('https', host, p, q, '')).rstrip('/') if not query else \
        urlunsplit(('https', host, p, q, ''))


def _first_non_locale(segs):
    for s in segs:
        if not LOCALE_RE.match(s):
            return s
    return None


def _q(qs, *names):
    """Return first present query param (case-insensitive) as {name: value}."""
    low = {k.lower(): (k, v) for k, v in qs.items()}
    out = {}
    for n in names:
        if n.lower() in low:
            k, v = low[n.lower()]
            out[k] = v[0] if isinstance(v, list) else v
    return out


# ---------------------------------------------------------------- handlers
def h_workday(host, segs, qs):
    board = None
    rest = [s for s in segs if not LOCALE_RE.match(s)]
    if rest and rest[0] == 'wday':
        # /wday/cxs/<tenant>/<board>/jobs  (API form)
        if len(rest) >= 4:
            board = rest[3]
    elif rest:
        board = rest[0]
    if board and board.lower() in ('job', 'details', 'jobs'):
        board = None
    tenant = host.split('.')[0]
    if board:
        return _build(host, [board]), tenant + '/' + board
    return _build(host), tenant


def h_greenhouse(host, segs, qs):
    tok = None
    f = _q(qs, 'for')
    if f:
        tok = list(f.values())[0]
    if not tok:
        rest = [s for s in segs if s.lower() not in ('embed', 'v1', 'boards', 'job_board', 'jobs')]
        if rest:
            tok = rest[0]
    if not tok:
        return None
    tok = tok.split('?')[0]
    h = 'job-boards.eu.greenhouse.io' if '.eu.' in host else 'job-boards.greenhouse.io'
    return _build(h, [tok]), tok


def h_lever(host, segs, qs):
    rest = [s for s in segs if s.lower() not in ('v0', 'postings')]
    if not rest:
        return None
    tok = rest[0]
    h = 'jobs.eu.lever.co' if '.eu.' in host else 'jobs.lever.co'
    return _build(h, [tok]), tok


def h_smartrecruiters(host, segs, qs):
    rest = [s for s in segs if s.lower() not in ('v1', 'companies', 'postings')]
    if not rest:
        return None
    return _build('careers.smartrecruiters.com', [rest[0]]), rest[0]


def h_jobvite(host, segs, qs):
    c = _q(qs, 'c', 'nl')
    if 'jobs.jobvite.com' in host and segs:
        return _build('jobs.jobvite.com', [segs[0]]), segs[0]
    if c:
        tok = list(c.values())[0]
        return _build('jobs.jobvite.com', [tok]), tok
    sub = host.split('.')[0]
    if sub not in ('www', 'app', 'jobs'):
        return _build(host), sub
    return None


def h_icims(host, segs, qs):
    tok = host.split('.')[0]
    if segs and segs[0].lower() == 'jobs':
        return _build(host, ['jobs', 'search']), tok
    return _build(host, ['jobs', 'search']), tok


def h_taleo(host, segs, qs):
    tok = host.split('.')[0]
    if 'careersection' in [s.lower() for s in segs]:
        i = [s.lower() for s in segs].index('careersection')
        keep = segs[:i + 2]
        return _build(host, keep), tok
    return _build(host, ['careersection']), tok


def h_oracle(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'sites' in low:
        i = low.index('sites')
        site = segs[i + 1] if len(segs) > i + 1 else None
        if site:
            return _build(host, ['hcmUI', 'CandidateExperience', 'en', 'sites', site]), site
    if 'hcmui' in low:
        return _build(host, segs[:5]), host.split('.')[0]
    return None


def h_successfactors(host, segs, qs):
    c = _q(qs, 'company')
    tok = list(c.values())[0] if c else host.split('.')[0]
    if c:
        return urlunsplit(('https', host, '/career', urlencode(c), '')), tok
    return _build(host, ['career']), tok


def h_ultipro(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'jobboard' in low:
        i = low.index('jobboard')
        return _build(host, segs[:i + 2]), '/'.join(segs[:i + 2])
    if segs:
        return _build(host, segs[:1]), segs[0]
    return _build(host), host.split('.')[0]


def h_adp(host, segs, qs):
    if 'myjobs.adp.com' in host:
        if segs:
            return _build(host, [segs[0], 'cx']), segs[0]
        return None
    keep = _q(qs, 'cid', 'ccId', 'ccid', 'lang')
    keep.pop('lang', None)
    if keep:
        return urlunsplit(('https', host, '/mascsr/default/mdf/recruitment/recruitment.html',
                           urlencode(keep), '')), list(keep.values())[0]
    return None


def h_dayforce(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'candidateportal' in low:
        i = low.index('candidateportal')
        return _build(host, segs[:i + 3]), host.split('.')[0]
    return _build(host), host.split('.')[0]


def h_paylocity(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'jobs' in low:
        i = low.index('jobs')
        return _build(host, segs[:i + 4]), '/'.join(segs[i:i + 4])
    return None


def h_paycom(host, segs, qs):
    k = _q(qs, 'clientkey')
    if k:
        return urlunsplit(('https', host, '/v4/ats/web.php/jobs', urlencode(k), '')), \
            list(k.values())[0]
    return None


def h_bamboo(host, segs, qs):
    return _build(host, ['careers']), host.split('.')[0]


def h_rippling(host, segs, qs):
    if 'ats.rippling.com' in host and segs:
        return _build(host, [segs[0], 'jobs']), segs[0]
    return _build(host), host.split('.')[0]


def h_workable(host, segs, qs):
    if 'apply.workable.com' in host and segs:
        return _build(host, [segs[0]]), segs[0]
    sub = host.split('.')[0]
    if sub not in ('www', 'apply', 'jobs'):
        return _build('apply.workable.com', [sub]), sub
    return None


def h_brassring(host, segs, qs):
    keep = _q(qs, 'partnerid', 'siteid', 'PartnerId', 'SiteId')
    tok = host.split('.')[0]
    if keep:
        return urlunsplit(('https', host, '/TGnewUI/Search/Home/Home', urlencode(keep), '')), tok
    return _build(host), tok


def h_csod(host, segs, qs):
    keep = _q(qs, 'c')
    low = [s.lower() for s in segs]
    tok = host.split('.')[0]
    if 'careersite' in low:
        i = low.index('careersite')
        base = segs[:i + 2] + ['home']
        if keep:
            return urlunsplit(('https', host, '/' + '/'.join(base), urlencode(keep), '')), tok
        return _build(host, base), tok
    return _build(host), tok


def h_neogov(host, segs, qs):
    if 'governmentjobs.com' in host:
        low = [s.lower() for s in segs]
        if 'careers' in low:
            i = low.index('careers')
            if len(segs) > i + 1:
                return _build(host, segs[:i + 2]), segs[i + 1]
        return None
    return _build(host), host.split('.')[0]


def h_careerplug(host, segs, qs):
    if 'app.careerplug.com' in host:
        low = [s.lower() for s in segs]
        if 'job_boards' in low:
            i = low.index('job_boards')
            return _build(host, segs[:i + 2]), segs[i + 1] if len(segs) > i + 1 else None
        return None
    return _build(host), host.split('.')[0]


def h_sub(host, segs, qs):
    """Generic: the subdomain IS the tenant; board lives at host root."""
    return _build(host), host.split('.')[0]


def h_sub_jobs(host, segs, qs):
    return _build(host, ['jobs']), host.split('.')[0]


def h_first_seg(host, segs, qs):
    if not segs:
        return None
    rest = [s for s in segs if not LOCALE_RE.match(s)]
    if not rest:
        return None
    return _build(host, [rest[0]]), rest[0]


def h_applicantstack(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'openings' in low:
        i = low.index('openings')
        return _build(host, segs[:i + 1]), host.split('.')[0]
    return _build(host, ['x', 'openings']), host.split('.')[0]


def h_root(host, segs, qs):
    return _build(host), host.split('.')[0]


def h_zohorecruit(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'jobs' in low:
        i = low.index('jobs')
        return _build(host, segs[:i + 2]), host.split('.')[0]
    return _build(host, ['jobs', 'Careers']), host.split('.')[0]


def h_eightfold(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'careers' in low:
        i = low.index('careers')
        return _build(host, segs[:i + 1]), host.split('.')[0]
    return _build(host, ['careers']), host.split('.')[0]


def h_hrmdirect(host, segs, qs):
    low = [s.lower() for s in segs]
    if 'careers' in low:
        i = low.index('careers')
        return _build(host, segs[:i + 2]), segs[i + 1] if len(segs) > i + 1 else host.split('.')[0]
    return _build(host), host.split('.')[0]


def h_isolved(host, segs, qs):
    return _build(host), host.split('.')[0]


def h_ashby(host, segs, qs):
    rest = [s for s in segs if s.lower() not in ('posting-api', 'job-board')]
    if not rest:
        return None
    return _build('jobs.ashbyhq.com', [rest[0]]), rest[0]


def h_recruitee(host, segs, qs):
    return _build(host), host.split('.')[0]


def h_jazzhr(host, segs, qs):
    return _build(host), host.split('.')[0]


def h_avature(host, segs, qs):
    low = [s.lower() for s in segs]
    for kw in ('careers', 'jobs', 'search'):
        if kw in low:
            i = low.index(kw)
            return _build(host, segs[:i + 1]), host.split('.')[0]
    return _build(host), host.split('.')[0]


def h_phenom(host, segs, qs):
    low = [s.lower() for s in segs]
    for kw in ('search-results', 'jobs', 'careers', 'search'):
        if kw in low:
            i = low.index(kw)
            return _build(host, segs[:i + 1]), host.split('.')[0]
    return _build(host), host.split('.')[0]


def h_appone(host, segs, qs):
    keep = _q(qs, 'ServerVar', 'CompanyId', 'companyid')
    if keep:
        return urlunsplit(('https', host, '/' + '/'.join(segs), urlencode(keep), '')), \
            list(keep.values())[0]
    return _build(host, segs[:2]) if segs else None


def h_paycor(host, segs, qs):
    # Paycor Recruiting boards live on *.newtonsoftware.com; paycor.com itself is
    # the vendor's own marketing site and is never an employer job board.
    if 'newtonsoftware.com' in host:
        low = [s.lower() for s in segs]
        if 'careers' in low:
            i = low.index('careers')
            return _build(host, segs[:i + 1]), host.split('.')[0]
        return _build(host, ['careers']), host.split('.')[0]
    if host.startswith('jobs.paycor.com') and segs:
        return _build(host, segs[:1]), segs[0]
    return None


R = re.compile
# (platform name, host regex, handler)
RULES = [
    ('Workday',              R(r'(^|\.)myworkdayjobs\.com$|(^|\.)myworkdaysite\.com$|(^|\.)wd\d+\.myworkdayjobs\.com$'), h_workday),
    ('Greenhouse',           R(r'(^|\.)greenhouse\.io$'), h_greenhouse),
    ('Lever',                R(r'(^|\.)lever\.co$'), h_lever),
    ('SmartRecruiters',      R(r'(^|\.)smartrecruiters\.com$'), h_smartrecruiters),
    ('Jobvite',              R(r'(^|\.)jobvite\.com$'), h_jobvite),
    ('iCIMS',                R(r'(^|\.)icims\.com$'), h_icims),
    ('Taleo',                R(r'(^|\.)taleo\.net$'), h_taleo),
    ('Oracle Recruiting Cloud', R(r'(^|\.)oraclecloud\.com$|(^|\.)oracle\.com$'), h_oracle),
    ('SAP SuccessFactors',   R(r'(^|\.)successfactors\.(com|eu)$|(^|\.)sapsf\.(com|eu)$|(^|\.)jobs\.sap\.com$'), h_successfactors),
    ('SAP Jobs2Web',         R(r'(^|\.)jobs2web\.com$'), h_sub),
    ('UKG / UltiPro',        R(r'(^|\.)ultipro\.com$|(^|\.)ukg\.(com|net)$'), h_ultipro),
    ('ADP',                  R(r'(^|\.)adp\.com$'), h_adp),
    ('Dayforce',             R(r'(^|\.)dayforcehcm\.com$|(^|\.)dayforce\.com$'), h_dayforce),
    ('Paylocity',            R(r'(^|\.)paylocity\.com$'), h_paylocity),
    ('Paycom',               R(r'(^|\.)paycomonline\.(net|com)$'), h_paycom),
    ('Paycor Recruiting',    R(r'(^|\.)newtonsoftware\.com$|(^|\.)paycor\.com$'), h_paycor),
    ('BambooHR',             R(r'(^|\.)bamboohr\.(com|co\.uk)$'), h_bamboo),
    ('Rippling',             R(r'(^|\.)rippling\.com$|(^|\.)rippling-ats\.com$'), h_rippling),
    ('Workable',             R(r'(^|\.)workable\.com$'), h_workable),
    ('ApplicantPro',         R(r'(^|\.)applicantpro\.com$|(^|\.)applicantpool\.com$'), h_sub_jobs),
    ('ApplicantStack',       R(r'(^|\.)applicantstack\.com$'), h_applicantstack),
    ('The Applicant Manager', R(r'(^|\.)theapplicantmanager\.com$'), h_sub),
    ('JazzHR',               R(r'(^|\.)applytojob\.com$|(^|\.)jazzhr\.com$'), h_jazzhr),
    ('Ashby',                R(r'(^|\.)ashbyhq\.com$'), h_ashby),
    ('Breezy HR',            R(r'(^|\.)breezy\.hr$'), h_recruitee),
    ('Cornerstone',          R(r'(^|\.)csod\.com$|(^|\.)cornerstoneondemand\.com$'), h_csod),
    ('BrassRing',            R(r'(^|\.)brassring\.com$|(^|\.)kenexa\.com$'), h_brassring),
    ('Avature',              R(r'(^|\.)avature\.net$'), h_avature),
    ('Pinpoint',             R(r'(^|\.)pinpointhq\.com$'), h_recruitee),
    ('Teamtailor',           R(r'(^|\.)teamtailor\.com$'), h_recruitee),
    ('Recruitee',            R(r'(^|\.)recruitee\.com$'), h_recruitee),
    ('CareerPlug',           R(r'(^|\.)careerplug\.com$'), h_careerplug),
    ('Hireology',            R(r'(^|\.)hireology\.com$'), h_sub),
    ('AcquireTM',            R(r'(^|\.)acquiretm\.com$'), h_sub),
    ('ViziRecruiter',        R(r'(^|\.)vizirecruiter\.com$'), h_sub),
    ('PrimePay Recruit',     R(r'(^|\.)primepay\.com$'), h_sub),
    ('AppOne',               R(r'(^|\.)appone\.com$'), h_appone),
    ('Appcast',              R(r'(^|\.)appcast\.io$'), h_root),
    ('JobApp Network',       R(r'(^|\.)jobappnetwork\.com$'), h_sub),
    ('isolved Hire',         R(r'(^|\.)isolvedhire\.com$'), h_isolved),
    ('SaaShr',               R(r'(^|\.)saashr\.com$'), h_root),
    ('HRMDirect',            R(r'(^|\.)hrmdirect\.com$'), h_hrmdirect),
    ('TalentReef',           R(r'(^|\.)talentreef\.com$|(^|\.)jobappnetwork\.com$'), h_sub),
    ('Eightfold',            R(r'(^|\.)eightfold\.ai$'), h_eightfold),
    ('Darwinbox',            R(r'(^|\.)darwinbox\.(in|com)$'), h_sub),
    ('Zoho Recruit',         R(r'(^|\.)zohorecruit\.(com|eu|in)$'), h_zohorecruit),
    ('SilkRoad',             R(r'(^|\.)silkroad\.com$|(^|\.)silkroadtech\.com$'), h_sub),
    ('Bullhorn',             R(r'(^|\.)bullhornstaffing\.com$|(^|\.)bullhorn\.com$|(^|\.)bullhorncloud\.com$'), h_sub),
    ('ClearCompany',         R(r'(^|\.)clearcompany\.com$'), h_sub),
    ('NeoGov',               R(r'(^|\.)neogov\.com$|(^|\.)governmentjobs\.com$'), h_neogov),
    ('Phenom',               R(r'(^|\.)phenompeople\.com$|(^|\.)phenom\.com$'), h_phenom),
    ('Oracle EBS iRecruitment', R(r'(^|\.)irecruitment\.'), h_root),
    # Additional widely used boards
    ('Personio',             R(r'(^|\.)personio\.(de|com)$|(^|\.)jobs\.personio\.de$'), h_sub),
    ('Recruiterbox/Trakstar', R(r'(^|\.)recruiterbox\.com$|(^|\.)trakstar\.com$|(^|\.)hire\.trakstar\.com$'), h_sub),
    ('Hirebridge',           R(r'(^|\.)hirebridge\.com$'), h_root),
    ('ExactHire',            R(r'(^|\.)exacthire\.com$'), h_sub),
    ('HiringThing',          R(r'(^|\.)hiringthing\.com$'), h_sub),
    ('Fountain',             R(r'(^|\.)fountain\.com$'), h_sub),
    ('Crelate',              R(r'(^|\.)crelate\.com$'), h_sub),
    ('JobDiva',              R(r'(^|\.)jobdiva\.com$'), h_root),
    ('Ceipal',              R(r'(^|\.)ceipal\.com$'), h_root),
    ('Gusto',                R(r'(^|\.)gusto\.com$'), h_first_seg),
    ('SmartSearch',          R(r'(^|\.)smartsearchonline\.com$'), h_root),
    ('Jobsoid',              R(r'(^|\.)jobsoid\.com$'), h_sub),
    ('GoHire',               R(r'(^|\.)gohire\.io$'), h_sub),
    ('Homerun',              R(r'(^|\.)homerun\.co$'), h_sub),
    ('Polymer',              R(r'(^|\.)polymer\.co$'), h_sub),
    ('Rec Solutions/Jobtarget', R(r'(^|\.)jobaps\.com$'), h_root),
    ('SuccessFactors RMK',   R(r'(^|\.)rmkcdn\.successfactors\.com$'), h_successfactors),
    ('Workstream',           R(r'(^|\.)workstream\.(is|us)$'), h_sub),
    ('Greenhouse Embed',     R(r'(^|\.)grnh\.se$'), h_greenhouse),
]


# A board URL must identify WHICH tenant it belongs to. Without that it is the
# provider's generic landing page (e.g. career4.successfactors.com/career), which
# is useless and gets attributed to several unrelated companies.
GENERIC_SEGS = {'career', 'careers', 'job', 'jobs', 'search', 'home', 'index', 'en', 'us',
                'en-us', 'default', 'main', 'portal', 'apply', 'openings', 'listing',
                'joblist', 'jobsearch', 'recruitment', 'candidateportal', 'cx'}
GENERIC_SUB_RE = re.compile(
    r'^(www\d*|careers?\d*|jobs?\d*|apply\d*|secure\d*|recruiting\d*|recruit\d*|hire\d*|'
    r'app\d*|api\d*|my|portal\d*|talent\d*|performancemanager\d*|web\d*|hcm\d*|'
    r'workforcenow|secure|jobs2web|krb|erecruit\d*)$', re.I)
IDENTITY_PARAMS = {'company', 'cid', 'ccid', 'clientkey', 'partnerid', 'siteid', 'c', 'for',
                   'servervar', 'companyid', 'organization', 'org', 'tenant', 'employer'}
# Provider-owned marketing/support subdomains that are never an employer's job board
NON_BOARD_SUBS = {'marketplace', 'blog', 'support', 'help', 'docs', 'developer', 'developers',
                  'status', 'community', 'learn', 'partners', 'partner', 'about', 'investors',
                  'news', 'shop', 'store', 'info', 'go', 'get', 'try', 'demo', 'events',
                  'training', 'academy', 'university', 'solutions', 'products', 'resources',
                  'pricing', 'contact', 'legal', 'privacy', 'security', 'trust', 'cdn',
                  'assets', 'static', 'media', 'images', 'downloads', 'sandbox', 'test'}


def has_tenant(url):
    """True if the URL identifies a specific employer tenant."""
    try:
        sp = urlsplit(url)
    except Exception:
        return False
    host = (sp.netloc or '').lower().split(':')[0]
    if not host:
        return False
    try:
        qs = parse_qs(sp.query)
    except Exception:
        qs = {}
    sub0 = host.split('.')[0]
    if sub0 in NON_BOARD_SUBS:
        return False
    if any(k.lower() in IDENTITY_PARAMS and v and str(v[0]).strip() for k, v in qs.items()):
        return True
    segs = [s for s in _segs(sp.path) if not LOCALE_RE.match(s)]
    if any(s.lower() not in GENERIC_SEGS and len(s) > 1 for s in segs):
        return True
    sub = host.split('.')[0]
    labels = host.split('.')
    # a multi-label host whose first label is company-specific (acme.bamboohr.com)
    if len(labels) >= 3 and not GENERIC_SUB_RE.match(sub):
        return True
    return False


def is_aggregator(host):
    host = (host or '').lower()
    for a in AGGREGATORS:
        if host == a or host.endswith('.' + a):
            return True
    return False


def detect(url):
    """Return {'platform','url','token','host'} if url belongs to a known ATS, else None."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip().strip('\'"),;')
    if not url.lower().startswith(('http://', 'https://')):
        if url.startswith('//'):
            url = 'https:' + url
        else:
            return None
    try:
        sp = urlsplit(url)
    except Exception:
        return None
    host = (sp.netloc or '').lower().split('@')[-1].split(':')[0]
    if not host or is_aggregator(host):
        return None
    # vendor marketing/support hosts (blog.workable.com, marketplace.paycor.com, ...)
    if host.split('.')[0] in NON_BOARD_SUBS:
        return None
    if BAD_PATH_RE.search(sp.path or ''):
        return None
    segs = _segs(sp.path)
    try:
        qs = parse_qs(sp.query)
    except Exception:
        qs = {}
    for name, rx, fn in RULES:
        if rx.search(host):
            try:
                res = fn(host, segs, qs)
            except Exception:
                res = None
            if res:
                canon, token = res
                if canon and has_tenant(canon):
                    return {'platform': name, 'url': canon, 'token': token, 'host': host}
                return None
    return None


def find_ats_in_text(text, limit=4000):
    """Scan raw HTML/JS/JSON for ATS URLs. Returns list of detection dicts."""
    out, seen = [], set()
    if not text:
        return out
    for m in URL_RE.finditer(text):
        if len(seen) > limit:
            break
        u = m.group(0)
        d = detect(u)
        if d and d['url'] not in seen:
            seen.add(d['url'])
            out.append(d)
    # Greenhouse embed token (`for: "acme"`) — ONLY when it sits next to a greenhouse
    # reference, otherwise plain HTML `<label for="...">` produces garbage tokens.
    low = (text or '').lower()
    gh_at = [m.start() for m in re.finditer(r'greenhouse', low)]
    if gh_at:
        for pat in (r'["\']?for["\']?\s*[:=]\s*["\']([A-Za-z0-9_-]{2,40})["\']',
                    r'Grnhse\.Settings[^;]{0,200}?["\']([A-Za-z0-9_-]{2,40})["\']'):
            for m in re.finditer(pat, text or ''):
                tok = m.group(1)
                if tok.lower() in ('true', 'false', 'null', 'undefined', 'email', 'name',
                                   'phone', 'message', 'subject', 'submit', 'search'):
                    continue
                if not any(abs(g - m.start()) < 400 for g in gh_at):
                    continue
                u = f'https://job-boards.greenhouse.io/{tok}'
                if u not in seen:
                    seen.add(u)
                    out.append({'platform': 'Greenhouse', 'url': u, 'token': tok,
                                'host': 'job-boards.greenhouse.io'})
    return out


def clean_generic_url(url):
    """Strip tracking/filter/session params from a non-ATS careers URL."""
    try:
        sp = urlsplit(url)
    except Exception:
        return url
    qs = parse_qs(sp.query)
    keep = {k: v for k, v in qs.items() if not STRIP_PARAMS.match(k)}
    return urlunsplit((sp.scheme, sp.netloc, sp.path.rstrip('/') or '/',
                       urlencode(keep, doseq=True), ''))


# ------------------------------------------------------- harvest endpoints
def api_endpoints(det):
    """Return list of (method, url, json_body, extractor_name) for job harvesting."""
    p, tok, host = det['platform'], det.get('token') or '', det['host']
    if p == 'Greenhouse':
        return [('GET', f'https://boards-api.greenhouse.io/v1/boards/{tok}/jobs', None, 'greenhouse')]
    if p == 'Lever':
        return [('GET', f'https://api.lever.co/v0/postings/{tok}?mode=json', None, 'lever')]
    if p == 'SmartRecruiters':
        return [('GET', f'https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=100',
                 None, 'smartrecruiters')]
    if p == 'Ashby':
        return [('GET', f'https://api.ashbyhq.com/posting-api/job-board/{tok}', None, 'ashby')]
    if p == 'Workable':
        return [('GET', f'https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true',
                 None, 'workable')]
    if p == 'Recruitee':
        return [('GET', f'https://{host}/api/offers/', None, 'recruitee')]
    if p == 'Breezy HR':
        return [('GET', f'https://{host}/json', None, 'breezy')]
    if p == 'BambooHR':
        return [('GET', f'https://{host}/careers/list', None, 'bamboo')]
    if p == 'Workday' and '/' in str(tok):
        tenant, board = str(tok).split('/', 1)
        return [('POST', f'https://{host}/wday/cxs/{tenant}/{board}/jobs',
                 {'appliedFacets': {}, 'limit': 20, 'offset': 0, 'searchText': ''}, 'workday')]
    if p == 'Teamtailor':
        return [('GET', f'https://{host}/jobs.json', None, 'teamtailor')]
    if p == 'Personio':
        return [('GET', f'https://{host}/xml', None, 'personio_xml')]
    return []
