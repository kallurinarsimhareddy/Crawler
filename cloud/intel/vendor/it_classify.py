# VENDORED from C:/Users/Narsimha Reddy/CareerAutomation/classify.py (not a git repo) on 2026-09-24 (pure logic, no network). Keep behaviour identical to the original.
"""IT/Technology job-title classification and US / Non-US company classification."""
import re

# --------------------------------------------------------------- IT titles
# DEFINITE: unambiguously IT/Technology -> counted even next to manufacturing words.
DEFINITE = [
    r'\bsoftware\s+(engineer|developer|architect|manager|director|intern|specialist|lead|test)',
    r'\bsoftware\s+quality', r'\bapplication[s]?\s+(developer|analyst|support|administrator)',
    r'\b(web|game|api|firmware)\s+(developer|engineer|programmer)',
    r'\bembedded\s+software', r'\bembedded\s+(developer|programmer)\b',
    r'\bfull[\s-]?stack\b', r'\bfront[\s-]?end\b', r'\bback[\s-]?end\b',
    r'\b(ios|android|mobile)\s+(developer|engineer)\b',
    r'\bdev\s?ops\b', r'\bdevsecops\b', r'\bsre\b', r'\bsite\s+reliability\b',
    r'\bplatform\s+engineer', r'\binfrastructure\s+(engineer|architect|analyst|manager|specialist)',
    r'\bcloud\s+(engineer|architect|developer|administrator|analyst|specialist|operations|security)',
    r'\b(aws|azure|gcp|kubernetes|docker|terraform)\b',
    r'\binformation\s+technology\b', r'\binformation\s+systems\b', r'\binformation\s+security\b',
    r'\bhelp\s?desk\b', r'\bservice\s?desk\b', r'\bdesktop\s+support\b',
    r'\btechnical\s+support\b', r'\btech\s+support\b', r'\bapplication\s+support\b',
    r'\b(systems?|server|linux|windows|unix|network|database|cloud)\s+admin',
    r'\bsysadmin\b', r'\bsystems?\s+analyst\b',
    r'\bnetwork\s+(engineer|architect|analyst|technician|specialist|administrator)',
    r'\bdatabase\s+(administrator|developer|engineer|architect|analyst)', r'\bdba\b',
    r'\b(sql|oracle|postgres|mysql|mongodb|snowflake|databricks)\b',
    r'\berp\b', r'\bsap\b', r'\bnetsuite\b', r'\bepicor\b', r'\binfor\b', r'\bjd\s?edwards\b',
    r'\bdynamics\s*365\b', r'\bmicrosoft\s+dynamics\b', r'\bworkday\b(?!\s+recruit)',
    r'\bsalesforce\b', r'\bservicenow\b', r'\bsharepoint\b', r'\bpower\s?bi\b', r'\btableau\b',
    r'\bbusiness\s+systems?\s+analyst', r'\bbusiness\s+intelligence\b', r'\bbi\s+(developer|analyst|engineer)',
    r'\bdata\s+(engineer|scientist|analyst|architect|warehouse|governance|platform)',
    r'\banalytics\b', r'\bmachine\s+learning\b', r'\bartificial\s+intelligence\b',
    r'\bml\s+(engineer|ops)\b', r'\bmlops\b', r'\bnlp\b', r'\bcomputer\s+vision\b',
    r'\bcyber\s?security\b', r'\bsecurity\s+(engineer|analyst|architect|administrator|operations)',
    r'\bsoc\s+analyst\b', r'\bpenetration\s+test', r'\bincident\s+response\b',
    r'\bqa\s+(engineer|analyst|automation|lead|manager|tester|specialist)',
    r'\btest\s+automation\b', r'\bsdet\b', r'\bsoftware\s+test',
    r'\bmes\s+(engineer|specialist|analyst|developer|administrator)',
    r'\bscrum\s+master\b', r'\bproduct\s+owner\b',
    r'\b(technical|solutions?|enterprise|data|software|cloud|security|integration)\s+architect',
    r'\bintegration\s+(engineer|developer|specialist|analyst)',
    r'\b(it|is)\s+(manager|director|analyst|specialist|technician|support|administrator|'
    r'coordinator|intern|lead|engineer|architect|security|operations|project\s+manager|business)',
    r'\b(director|manager|vp|head)\s+of\s+(it|information\s+technology|engineering|software|'
    r'data|analytics|cyber|digital|technology)',
    r'\bcio\b', r'\bcto\b', r'\bciso\b',
    r'\bdigital\s+(transformation|product|platform)\b',
    r'\bux\s+(designer|researcher|engineer)\b', r'\bui\s?/?\s?ux\b',
    r'\bcrm\s+(administrator|analyst|developer|manager)',
    r'\bpython\b', r'\bjavascript\b', r'\b\.net\b', r'\bc\+\+\b', r'\breact\s+developer\b',
    r'\bedi\s+(analyst|specialist|developer|coordinator)',
    r'\bpki\b', r'\bidentity\s+and\s+access\b', r'\bit\s+asset\b',
    r'\bhelp\s?desk\b', r'\bnoc\s+(technician|engineer|analyst)\b',
]

# AMBIGUOUS: IT-ish, but a manufacturing/trade context word vetoes it
# ("CNC Programmer", "PLC Programmer", "Controls Automation Engineer" are NOT IT).
AMBIGUOUS = [
    r'\bprogrammer\b', r'\bprogramming\b',
    r'\bautomation\s+(engineer|developer|architect|specialist|analyst)\b',
    r'\bsystems?\s+engineer\b', r'\bsolutions?\s+engineer\b',
    r'\bquality\s+assurance\s+(engineer|analyst|architect)\b',
    r'\btest\s+(engineer|analyst)\b',
    r'\bscada\s+(engineer|developer|software|specialist)\b',
]

# Weak/token signals requiring a role-word nearby (also vetoed by EXCLUDE)
WEAK_TOKENS = r'\b(technology|technical|digital|systems?)\b'

EXCLUDE = [
    r'\bcnc\b', r'\bplc\s+programmer\b', r'\bmachine\s+operator\b', r'\bmachinist\b',
    r'\boperator\b', r'\bproduction\s+(worker|associate|supervisor|manager|planner|scheduler|lead|technician)',
    r'\bmanufacturing\s+(technician|associate|operator|worker)',
    r'\belectrician\b', r'\bsecurity\s+(guard|officer)\b', r'\bwarehouse\b', r'\bassembler\b',
    r'\bassembly\b', r'\bquality\s+inspector\b', r'\binspector\b', r'\bwelder\b', r'\bwelding\b',
    r'\bforklift\b', r'\bmaterial\s+handler\b', r'\bpicker\b', r'\bpacker\b', r'\bshipping\b',
    r'\breceiving\b', r'\bjanitor\b', r'\bcustodian\b', r'\bcnc\s+machinist\b',
    r'\bmaintenance\s+(technician|mechanic|worker)\b', r'\bmillwright\b', r'\bfabricator\b',
    r'\btool\s*(and|&)\s*die\b', r'\bpress\s+operator\b', r'\bmolding\b', r'\bpainter\b',
    r'\bdriver\b', r'\bcdl\b', r'\btruck\b', r'\bnurse\b', r'\bcashier\b', r'\bserver\b(?!\s*(admin|engineer))',
    r'\bcook\b', r'\bcleaner\b', r'\bsanitation\b', r'\bgrinder\b', r'\blathe\b', r'\bdeburr',
    r'\bmechanical\s+(engineer|designer|technician)\b', r'\belectrical\s+(engineer|technician)\b',
    r'\bchemical\s+engineer\b', r'\bindustrial\s+engineer\b', r'\bcivil\s+engineer\b',
    r'\bprocess\s+engineer\b', r'\bmanufacturing\s+engineer\b', r'\bdesign\s+engineer\b',
    r'\bfield\s+service\s+technician\b', r'\bfield\s+technician\b',
    r'\bhr\b', r'\bhuman\s+resources\b', r'\baccountant\b', r'\baccounting\b', r'\bpayroll\b',
    r'\bsales\s+(representative|manager|associate|rep)\b', r'\bmarketing\s+(manager|coordinator|specialist)\b',
    r'\bcustomer\s+service\b', r'\breceptionist\b', r'\badministrative\s+assistant\b',
    r'\bbuyer\b', r'\bplanner\b', r'\bestimator\b', r'\bdraftsman\b', r'\bcad\s+(designer|drafter|technician)\b',
    r'\bintern\s*-\s*(mechanical|electrical|manufacturing|chemical)',
    r'\bapprentice\b', r'\blaborer\b', r'\bgeneral\s+labor\b', r'\btechnician\s+i+\b',
    r'\bcontrols?\s+(engineer|technician|specialist)\b', r'\brobotics?\b', r'\btooling\b',
    r'\bmold(ing)?\b', r'\bmachining\b', r'\binstrumentation\b', r'\bplc\b', r'\bhydraulic',
    r'\bpneumatic', r'\bextrusion\b', r'\bfoundry\b', r'\bcasting\b', r'\bstamping\b',
    r'\bhvac\b', r'\bplumb', r'\bcarpent', r'\bconstruction\b', r'\bsurvey(or|ing)\b',
    r'\bmetrolog', r'\bcalibration\b', r'\bchemist\b', r'\blaboratory\b', r'\blab\s+tech',
    r'\bfood\s+safety\b', r'\bsanitation\b', r'\bmachine\s+shop\b',
]

DEFINITE_RE = [re.compile(p, re.I) for p in DEFINITE]
AMBIGUOUS_RE = [re.compile(p, re.I) for p in AMBIGUOUS]
EXCLUDE_RE = [re.compile(p, re.I) for p in EXCLUDE]
IT_TOKEN_RE = re.compile(r'(^|[\s\-\|/(,])IT([\s\-\|/),.]|$)')  # case-sensitive "IT"
WEAK_RE = re.compile(WEAK_TOKENS, re.I)
ROLE_RE = re.compile(r'\b(engineer|developer|analyst|architect|administrator|manager|director|'
                     r'specialist|consultant|lead|intern|technician|support|coordinator|'
                     r'programmer|scientist|officer)\b', re.I)

NOISE_TITLES = re.compile(
    r'^(view all|see all|all jobs|search|apply|apply now|learn more|read more|home|careers?|jobs?|'
    r'open positions?|current openings?|back|next|previous|filter|clear|submit|sign in|log ?in|'
    r'menu|close|contact us|about us?|privacy|terms|cookie|share|email|print|more info|'
    r'load more|show more|browse|explore|benefits|culture|our team|life at .*|no results.*|'
    r'\d+|page \d+)$', re.I)


def is_it_title(title):
    """True if the job title denotes an IT/Technology role.

    DEFINITE terms win outright. AMBIGUOUS / weak terms are vetoed by a
    manufacturing-or-trade EXCLUDE term in the same title.
    """
    if not title:
        return False
    t = ' ' + re.sub(r'\s+', ' ', str(title)).strip() + ' '
    for rx in DEFINITE_RE:
        if rx.search(t):
            return True
    if IT_TOKEN_RE.search(t):
        return True
    soft = any(rx.search(t) for rx in AMBIGUOUS_RE) or \
        (WEAK_RE.search(t) and ROLE_RE.search(t))
    if soft:
        return not any(rx.search(t) for rx in EXCLUDE_RE)
    return False


def classify_titles(titles):
    """Return (it_titles, all_clean_titles)."""
    clean, it = [], []
    seen = set()
    for raw in titles:
        if not raw:
            continue
        t = re.sub(r'\s+', ' ', str(raw)).strip(' \t\n\r-|·•,')
        if not t or len(t) < 3 or len(t) > 140:
            continue
        if NOISE_TITLES.match(t):
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        clean.append(t)
        if is_it_title(t):
            it.append(t)
    return it, clean


# ------------------------------------------------------------ US / Non-US
GENERIC_CC = {'io', 'ai', 'co', 'me', 'tv', 'cc', 'fm', 'ly', 'sh', 'gg', 'to', 'st',
              'gl', 'am', 'fo', 'is', 'la', 'ms', 'nu', 'tk', 'vc', 'ws'}

CC_COUNTRY = {
    'uk': 'United Kingdom', 'gb': 'United Kingdom', 'de': 'Germany', 'fr': 'France',
    'it': 'Italy', 'es': 'Spain', 'nl': 'Netherlands', 'be': 'Belgium', 'ch': 'Switzerland',
    'at': 'Austria', 'se': 'Sweden', 'no': 'Norway', 'dk': 'Denmark', 'fi': 'Finland',
    'ie': 'Ireland', 'pt': 'Portugal', 'pl': 'Poland', 'cz': 'Czechia', 'ru': 'Russia',
    'in': 'India', 'cn': 'China', 'jp': 'Japan', 'kr': 'South Korea', 'tw': 'Taiwan',
    'sg': 'Singapore', 'my': 'Malaysia', 'th': 'Thailand', 'id': 'Indonesia', 'ph': 'Philippines',
    'au': 'Australia', 'nz': 'New Zealand', 'ca': 'Canada', 'mx': 'Mexico', 'br': 'Brazil',
    'ar': 'Argentina', 'cl': 'Chile', 'za': 'South Africa', 'ae': 'UAE', 'il': 'Israel',
    'tr': 'Turkey', 'gr': 'Greece', 'hu': 'Hungary', 'ro': 'Romania', 'sk': 'Slovakia',
    'si': 'Slovenia', 'hr': 'Croatia', 'bg': 'Bulgaria', 'ua': 'Ukraine', 'hk': 'Hong Kong',
    'vn': 'Vietnam', 'sa': 'Saudi Arabia', 'eg': 'Egypt', 'ng': 'Nigeria', 'ke': 'Kenya',
    'pe': 'Peru', 'ec': 'Ecuador', 'uy': 'Uruguay', 'lu': 'Luxembourg', 'ee': 'Estonia',
    'lv': 'Latvia', 'lt': 'Lithuania', 'rs': 'Serbia', 'by': 'Belarus', 'kz': 'Kazakhstan',
    'pk': 'Pakistan', 'bd': 'Bangladesh', 'lk': 'Sri Lanka', 'qa': 'Qatar', 'kw': 'Kuwait',
}

US_STATES = (r'\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|'
             r'MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b')
US_STATE_RE = re.compile(US_STATES)
US_ZIP_RE = re.compile(r'\b\d{5}(-\d{4})?\b')
US_WORDS_RE = re.compile(r'\b(united states|u\.s\.a?\.?|usa\b|america\b)', re.I)
US_STATE_NAMES = re.compile(
    r'\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|'
    r'georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|'
    r'massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|'
    r'new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|'
    r'oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|'
    r'vermont|virginia|washington|west virginia|wisconsin|wyoming)\b', re.I)

NON_US_COUNTRY_RE = re.compile(
    r'\b(united kingdom|england|scotland|wales|ireland|germany|deutschland|france|italy|italia|'
    r'spain|españa|netherlands|nederland|belgium|switzerland|schweiz|austria|sweden|sverige|'
    r'norway|denmark|finland|portugal|poland|polska|czech|slovakia|slovenia|croatia|hungary|'
    r'romania|bulgaria|greece|turkey|türkiye|russia|ukraine|india|bharat|china|中国|japan|日本|'
    r'south korea|taiwan|singapore|malaysia|thailand|indonesia|philippines|vietnam|australia|'
    r'new zealand|canada|mexico|méxico|brazil|brasil|argentina|chile|colombia|peru|'
    r'south africa|israel|united arab emirates|saudi arabia|qatar|kuwait|egypt|nigeria|kenya|'
    r'pakistan|bangladesh|sri lanka|hong kong|luxembourg|estonia|latvia|lithuania|serbia)\b', re.I)


def tld_country(suffix):
    """Return country name if the public suffix is a clearly national ccTLD, else None."""
    if not suffix:
        return None
    last = suffix.split('.')[-1].lower()
    if len(last) != 2 or last == 'us' or last in GENERIC_CC:
        return None
    return CC_COUNTRY.get(last, 'Non-US (.%s)' % last)


def location_is_us(loc):
    if not loc:
        return None
    s = str(loc)
    if NON_US_COUNTRY_RE.search(s):
        return False
    if US_WORDS_RE.search(s) or US_STATE_NAMES.search(s) or US_STATE_RE.search(s) or US_ZIP_RE.search(s):
        return True
    if re.search(r'\bremote\b', s, re.I):
        return None
    return None


def classify_country(suffix, page_text, job_locations):
    """Return (is_non_us: bool, reason: str). Only True at >95% confidence."""
    c = tld_country(suffix)
    if c:
        return True, f'ccTLD suffix .{suffix} -> {c}'

    txt = (page_text or '')[:200000]
    us_hits = len(US_STATE_NAMES.findall(txt)) + len(US_WORDS_RE.findall(txt)) + \
        len(US_ZIP_RE.findall(txt))
    non_us_hits = len(NON_US_COUNTRY_RE.findall(txt))

    locs = [l for l in (job_locations or []) if l]
    if len(locs) >= 3:
        verdicts = [location_is_us(l) for l in locs]
        known = [v for v in verdicts if v is not None]
        if len(known) >= 3 and all(v is False for v in known):
            return True, f'all {len(known)} job locations non-US'
        if any(v is True for v in known):
            return False, 'US job locations present'

    if non_us_hits >= 6 and us_hits == 0:
        return True, f'site text: {non_us_hits} non-US country refs, 0 US refs'
    return False, 'defaults to US'
