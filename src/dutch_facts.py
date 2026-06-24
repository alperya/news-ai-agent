"""
Daily "Did you know?" Dutch-fact pool + rotation.

Powers the daily Instagram Story (``format: daily_fact``). Each entry is a
short, shareable, English fact about the Netherlands plus Pexels search
queries that drive the background B-roll.

Rotation is stateless (no DB) — state lives in S3:
  • ``facts/pool.json``     — the live fact pool (hot-editable, no redeploy).
                              Seeded from DEFAULT_FACTS on first run.
  • ``facts/_rotation.json``— {"used": {id: "YYYY-MM-DD"}, "cycle_alerted": bool}

Each day the next unused fact is picked in curated pool order (strongest facts
first); once every fact has aired, the least-recently-used one repeats, so a
fact only comes back after roughly the whole pool has been shown (~pool-size days).
When the pool is nearly cycled, a one-time SNS reminder is emailed so new
facts can be appended to ``pool.json``.
"""

import json
import logging
from datetime import date

import boto3
from botocore.exceptions import ClientError

from notifier import send_alert

logger = logging.getLogger(__name__)

_POOL_KEY = "facts/pool.json"
_ROTATION_KEY = "facts/_rotation.json"
_REFILL_THRESHOLD = 7  # warn when fewer than this many unused facts remain


# ── Seed pool (English, ≤~20 words, shareable) ────────────────────────────────
# Stats are accurate as of authoring; verify before large edits.
DEFAULT_FACTS = [
    {"id": "bikes-outnumber-people", "text": "The Netherlands has more bicycles than people, with around 23 million bikes for 18 million Dutch.", "footage_queries": ["amsterdam bicycles canal", "netherlands cycling city", "bikes parked street"]},
    {"id": "second-ag-exporter", "text": "Despite its small size, the Netherlands is the world's second-largest exporter of agricultural products.", "footage_queries": ["dutch greenhouse tomatoes", "netherlands farmland aerial", "agriculture harvest field"]},
    {"id": "below-sea-level", "text": "About a third of the Netherlands lies below sea level, and the lowest point is 6.76 metres down.", "footage_queries": ["dutch dike polder", "netherlands water landscape", "aerial polder fields"]},
    {"id": "tallest-people", "text": "The Dutch are the tallest people on Earth, with men averaging around 1.83 metres.", "footage_queries": ["amsterdam people walking", "netherlands crowd street", "tall person city"]},
    {"id": "orange-carrots", "text": "Carrots were purple and yellow until Dutch growers popularised the orange variety we eat today.", "footage_queries": ["orange carrots market", "vegetable market netherlands", "fresh carrots harvest"]},
    {"id": "schiphol-below-sea", "text": "Schiphol Airport sits 3.4 metres below sea level, built on the bed of a drained lake.", "footage_queries": ["airport runway plane", "schiphol amsterdam airport", "airplane landing"]},
    {"id": "capital-vs-government", "text": "Amsterdam is the capital, but the Dutch government actually sits in The Hague.", "footage_queries": ["the hague government building", "amsterdam canal houses", "dutch parliament"]},
    {"id": "oldest-stock-exchange", "text": "Amsterdam hosted the world's first modern stock exchange, founded in 1602 for the Dutch East India Company.", "footage_queries": ["amsterdam old building", "historic stock exchange", "amsterdam canal historic"]},
    {"id": "kings-day-orange", "text": "On King's Day the whole country turns orange and Amsterdam's canals fill with party boats.", "footage_queries": ["kings day amsterdam orange", "canal boats party", "netherlands festival crowd"]},
    {"id": "kinderdijk-windmills", "text": "The 19 historic windmills of Kinderdijk are a UNESCO World Heritage site.", "footage_queries": ["kinderdijk windmills", "dutch windmill landscape", "windmill canal netherlands"]},
    {"id": "flevoland-reclaimed", "text": "Flevoland, an entire Dutch province, was reclaimed from the sea and is the world's largest artificial island.", "footage_queries": ["netherlands aerial polder", "flevoland farmland", "reclaimed land water"]},
    {"id": "jenever-gin", "text": "Gin descends from Dutch jenever, and the phrase 'Dutch courage' comes from drinking it.", "footage_queries": ["jenever glass bar", "distillery bottles", "amsterdam pub drink"]},
    {"id": "first-same-sex-marriage", "text": "In 2001 the Netherlands became the first country in the world to legalise same-sex marriage.", "footage_queries": ["amsterdam pride canal", "rainbow flag city", "netherlands celebration"]},
    {"id": "cycle-path-length", "text": "The Netherlands has roughly 35,000 kilometres of dedicated cycle paths.", "footage_queries": ["dutch cycle path", "bike lane netherlands", "cycling countryside"]},
    {"id": "tulip-mania", "text": "The world's first speculative bubble was Dutch 'Tulip Mania' in 1637, when bulbs cost more than houses.", "footage_queries": ["tulip fields netherlands", "colorful tulips", "flower field aerial"]},
    {"id": "van-gogh-one-painting", "text": "Van Gogh sold barely a painting in his lifetime; his Amsterdam museum now draws millions yearly.", "footage_queries": ["amsterdam museum", "art gallery painting", "museum visitors"]},
    {"id": "english-proficiency", "text": "The Dutch consistently rank first worldwide in English proficiency among non-native speakers.", "footage_queries": ["amsterdam people talking", "netherlands university students", "city conversation"]},
    {"id": "rotterdam-port", "text": "Rotterdam is the largest seaport in Europe and was the world's busiest for decades.", "footage_queries": ["rotterdam port ships", "container harbor cranes", "cargo ship sea"]},
    {"id": "afsluitdijk", "text": "The 32-kilometre Afsluitdijk dam turned a stormy sea inlet into the calm IJsselmeer lake in 1932.", "footage_queries": ["afsluitdijk dam road", "netherlands sea dike", "long causeway water"]},
    {"id": "wind-powered-trains", "text": "Since 2017 all Dutch electric trains have run entirely on wind energy.", "footage_queries": ["dutch train station", "wind turbines netherlands", "train passing countryside"]},
    {"id": "museums-density", "text": "The Netherlands has one of the highest densities of museums per square kilometre in the world.", "footage_queries": ["amsterdam museum building", "art museum interior", "rijksmuseum"]},
    {"id": "low-countries-name", "text": "'Nederland' literally means 'low country', and about a quarter of it sits below sea level.", "footage_queries": ["netherlands flat landscape", "polder horizon", "dutch countryside"]},
    {"id": "oldest-anthem", "text": "The Dutch anthem, the Wilhelmus, is widely considered the oldest national anthem in the world.", "footage_queries": ["netherlands flag waving", "dutch historic town", "amsterdam old square"]},
    {"id": "maeslantkering", "text": "Near Rotterdam, the Maeslantkering is one of the largest moving structures on Earth: two giant arms that swing shut automatically to hold back North Sea storm surges.", "footage_queries": ["storm surge barrier", "rotterdam flood defense", "huge steel structure water"]},
    {"id": "cheese-exporter", "text": "Gouda and Edam are named after market towns, and the Netherlands is the world's top cheese exporter.", "footage_queries": ["dutch cheese market", "gouda cheese wheels", "cheese shop netherlands"]},
    {"id": "almere-new-city", "text": "Almere, home to over 200,000 people, didn't exist 50 years ago and is built on reclaimed seabed.", "footage_queries": ["almere modern city", "new dutch architecture", "planned city aerial"]},
    {"id": "coffee-drinkers", "text": "The Dutch are among the world's heaviest coffee drinkers per person.", "footage_queries": ["coffee cup cafe", "amsterdam coffee shop", "pouring coffee"]},
    {"id": "delta-works", "text": "The Delta Works flood-defence system is called one of the Seven Wonders of the Modern World.", "footage_queries": ["delta works netherlands", "sea barrier dam", "coastal engineering"]},
    {"id": "hagelslag-breakfast", "text": "The Dutch sprinkle chocolate 'hagelslag' on buttered bread for breakfast, even the adults.", "footage_queries": ["chocolate sprinkles bread", "dutch breakfast table", "hagelslag toast"]},
    {"id": "stroopwafel-origin", "text": "The stroopwafel, two thin waffles glued together with caramel syrup, was invented in Gouda.", "footage_queries": ["stroopwafel caramel", "dutch waffle market", "stroopwafel coffee"]},
    {"id": "tulip-export", "text": "The Netherlands grows and exports the vast majority of the world's flower bulbs.", "footage_queries": ["tulip field rows", "flower auction netherlands", "bulb fields aerial"]},
    {"id": "queens-to-kings", "text": "After 123 years of three successive queens, the Netherlands got a king again in 2013.", "footage_queries": ["dutch royal palace", "amsterdam dam square", "netherlands monarchy"]},
    {"id": "no-stray-dogs", "text": "The Netherlands is said to be the first country to have effectively no stray dogs.", "footage_queries": ["dog park netherlands", "happy dog city", "person walking dog amsterdam"]},
    {"id": "canals-amsterdam", "text": "Amsterdam has more than 100 kilometres of canals, around 90 islands and 1,500 bridges.", "footage_queries": ["amsterdam canals aerial", "canal bridge amsterdam", "boat tour canal"]},
    {"id": "giethoorn-no-roads", "text": "Giethoorn, the 'Venice of the North', has canals and footbridges instead of roads in its old centre.", "footage_queries": ["giethoorn village canal", "boat thatched cottage", "dutch water village"]},
    {"id": "highest-bike-use", "text": "More than a quarter of all trips in the Netherlands are made by bicycle, the highest rate on Earth.", "footage_queries": ["commuters cycling", "bike traffic netherlands", "cyclists crossing"]},
    {"id": "north-sea-protection", "text": "Massive dunes, dikes and pumps protect millions of Dutch people living below sea level.", "footage_queries": ["sand dunes coast", "sea dike waves", "netherlands beach defense"]},
    {"id": "windmill-purpose", "text": "Historic Dutch windmills did much more than grind grain. Many pumped water to keep the land dry.", "footage_queries": ["windmill turning blades", "dutch windmill water", "historic windmill field"]},
    {"id": "amsterdam-narrow-houses", "text": "Amsterdam's canal houses are famously narrow because owners were once taxed on facade width.", "footage_queries": ["amsterdam canal houses", "narrow dutch house", "historic facades canal"]},
    {"id": "liberation-tulips", "text": "Canada gets thousands of tulips from the Netherlands every year as thanks for WWII liberation.", "footage_queries": ["tulips bouquet", "tulip field spring", "colorful tulips garden"]},
    {"id": "dutch-doors", "text": "The 'Dutch door' is split across the middle so the top half can open for light and air while the bottom stays shut to keep children or animals inside.", "footage_queries": ["old farmhouse door", "dutch countryside house", "wooden door cottage"]},
    {"id": "bicycle-parking-utrecht", "text": "Utrecht built the world's largest bicycle parking garage, holding over 12,000 bikes.", "footage_queries": ["bicycle parking garage", "utrecht station bikes", "rows of bicycles"]},
    {"id": "below-sea-pumps", "text": "Without constant pumping, much of the western Netherlands would flood within days.", "footage_queries": ["water pumping station", "dutch polder canal", "drainage netherlands"]},
    {"id": "windmill-day", "text": "The Netherlands celebrates a National Mill Day when hundreds of historic mills open to visitors.", "footage_queries": ["dutch windmill sunny", "windmills row landscape", "historic mill netherlands"]},
    {"id": "first-multinational", "text": "The Dutch East India Company was arguably the world's first multinational corporation.", "footage_queries": ["amsterdam historic harbor", "old sailing ship", "vintage map sea"]},
    {"id": "speed-skating", "text": "The Dutch dominate Olympic speed skating, winning a remarkable share of all its medals.", "footage_queries": ["speed skating ice", "ice skating netherlands", "frozen canal skating"]},
    {"id": "elfstedentocht", "text": "The Elfstedentocht is a 200 kilometre ice skating race past eleven Frisian towns, held only in the rare winters when every canal on the route freezes solid enough.", "footage_queries": ["frozen canal skaters", "ice skating tour", "winter netherlands canal"]},
    {"id": "polder-model", "text": "The Dutch love settling disagreements through patient compromise, a habit named the 'polder model' after the centuries of teamwork once needed to drain and protect their low-lying land.", "footage_queries": ["dutch meeting office", "netherlands business people", "discussion table"]},
    {"id": "north-sea-jazz", "text": "Rotterdam hosts North Sea Jazz, one of the largest indoor music festivals in the world.", "footage_queries": ["jazz concert stage", "music festival crowd", "saxophone performance"]},
    {"id": "dutch-light-painters", "text": "The unique 'Dutch light' over the flat landscape inspired masters like Vermeer and Rembrandt.", "footage_queries": ["dutch sky clouds", "flat landscape sunset", "netherlands golden light"]},
    {"id": "keukenhof", "text": "Keukenhof plants around 7 million flower bulbs by hand each year for its spring display.", "footage_queries": ["keukenhof gardens tulips", "flower garden netherlands", "tulip park spring"]},
    {"id": "happiest-country", "text": "The Netherlands is regularly ranked among the five happiest countries in the world, year after year.", "footage_queries": ["happy people netherlands", "friends laughing outdoors", "amsterdam people smiling"]},
    {"id": "amsterdam-bikes-in-canals", "text": "Thousands of bicycles are fished out of Amsterdam's canals every single year.", "footage_queries": ["amsterdam canal bikes", "bicycle by canal", "canal boat amsterdam"]},
    {"id": "dutch-treat", "text": "'Going Dutch', or splitting the bill, is named after the Netherlands' practical reputation.", "footage_queries": ["restaurant table bill", "cafe friends netherlands", "paying restaurant"]},
    {"id": "highest-broadband", "text": "The Netherlands has some of the highest broadband internet coverage in the world.", "footage_queries": ["modern office netherlands", "fiber cables tech", "person laptop cafe"]},
    {"id": "rembrandt-night-watch", "text": "Rembrandt's giant 'Night Watch' is the centrepiece of Amsterdam's Rijksmuseum.", "footage_queries": ["rijksmuseum amsterdam", "classic painting museum", "art gallery visitors"]},
    {"id": "windmill-count", "text": "Around a thousand traditional windmills still stand across the Netherlands today.", "footage_queries": ["windmills netherlands landscape", "dutch windmill field", "windmill river"]},
    {"id": "anne-frank-house", "text": "Anne Frank's hidden annex in Amsterdam is now one of the most visited museums in the Netherlands.", "footage_queries": ["amsterdam canal house", "historic amsterdam street", "old building facade"]},
    {"id": "dutch-bicycles-design", "text": "The upright 'omafiets' grandma bike is the classic Dutch design built for comfort, not speed.", "footage_queries": ["vintage dutch bicycle", "classic bike street", "bicycle basket flowers"]},
    {"id": "land-from-sea", "text": "Roughly a sixth of the Netherlands' total land area was reclaimed from water.", "footage_queries": ["polder aerial view", "reclaimed farmland", "netherlands water land"]},
    {"id": "cheese-per-capita", "text": "The Dutch eat enormous amounts of cheese, among the highest consumption per person worldwide.", "footage_queries": ["cheese platter", "dutch cheese market stall", "cheese wheels shop"]},
    {"id": "windmill-unesco-zaanse", "text": "Zaanse Schans preserves working windmills and green wooden houses just outside Amsterdam.", "footage_queries": ["zaanse schans windmills", "green wooden houses", "windmill river netherlands"]},
    {"id": "delft-blue", "text": "Delftware's blue-and-white pottery has been made in the city of Delft since the 1600s.", "footage_queries": ["delft blue pottery", "ceramic painting", "dutch porcelain"]},
    {"id": "highest-museum-art", "text": "The Mauritshuis in The Hague holds Vermeer's 'Girl with a Pearl Earring'.", "footage_queries": ["the hague museum", "classic dutch painting", "art gallery"]},
    {"id": "biking-to-school", "text": "Most Dutch children cycle to school on their own from a young age.", "footage_queries": ["children cycling", "kids bikes street", "school bicycles netherlands"]},
    {"id": "north-sea-wind", "text": "The Netherlands is building huge offshore wind farms in the North Sea to power millions of homes.", "footage_queries": ["offshore wind turbines sea", "wind farm ocean", "wind turbine aerial"]},
    {"id": "dutch-pancakes", "text": "Dutch 'pannenkoeken' are thin, plate-sized pancakes served with sweet or savoury toppings.", "footage_queries": ["dutch pancake plate", "pancake restaurant", "cooking pancake pan"]},
    {"id": "amsterdam-bikes-parking", "text": "Amsterdam Central Station has underwater bicycle parking for thousands of bikes.", "footage_queries": ["amsterdam central station", "bicycle parking bikes", "station bikes netherlands"]},
    {"id": "windmill-power-history", "text": "By the 1700s the Dutch ran thousands of windmills, an early industrial powerhouse.", "footage_queries": ["historic windmills row", "windmill landscape sunset", "old dutch mill"]},
    {"id": "frites-mayo", "text": "In the Netherlands, fries ('patat') are traditionally eaten with mayonnaise, not ketchup.", "footage_queries": ["dutch fries mayonnaise", "fries cone snack", "fast food netherlands"]},
    {"id": "bitterballen", "text": "Bitterballen, crispy deep fried meatballs, are the Netherlands' favourite bar snack.", "footage_queries": ["bitterballen snack", "fried food dutch bar", "pub snack netherlands"]},
    {"id": "ns-network", "text": "The Dutch railway network is one of the busiest and most punctual in Europe.", "footage_queries": ["dutch train platform", "train station netherlands", "intercity train"]},
    {"id": "windmill-de-gooyer", "text": "Amsterdam's De Gooyer windmill stands beside a brewery in a former public bathhouse.", "footage_queries": ["amsterdam windmill city", "windmill urban", "de gooyer brewery"]},
    {"id": "highest-life-bikes", "text": "Cycling so much helps make the Dutch some of the healthiest and longest-living people in Europe.", "footage_queries": ["elderly person cycling", "active people netherlands", "cycling park"]},
    {"id": "tulip-from-turkey", "text": "Tulips aren't originally Dutch. They came from the Ottoman Empire before booming in the Netherlands.", "footage_queries": ["tulip close up", "tulip field colorful", "spring flowers"]},
    {"id": "haring-herring", "text": "Eating raw 'Hollandse Nieuwe' herring, tilted into your mouth, is a Dutch summer tradition.", "footage_queries": ["dutch herring stall", "raw herring snack", "fish market netherlands"]},
    {"id": "amsterdam-houseboats", "text": "Thousands of people in Amsterdam live full-time on houseboats moored along the canals.", "footage_queries": ["amsterdam houseboat canal", "boat homes water", "canal living amsterdam"]},
    {"id": "windmill-sails-signal", "text": "Long before telephones, a miller could rest the windmill's sails at set angles to send a message across the flat land, announcing a birth, a death or danger nearby.", "footage_queries": ["windmill sails sky", "dutch windmill close", "windmill silhouette"]},
    {"id": "philips-eindhoven", "text": "Electronics giant Philips was founded in Eindhoven in 1891 and shaped the modern light bulb.", "footage_queries": ["eindhoven city tech", "light bulbs", "modern electronics lab"]},
    {"id": "polder-windmills-drain", "text": "A single windmill can only lift water about a metre, so the Dutch chained them together, each one pumping the water a step higher, until whole lakes drained into dry farmland.", "footage_queries": ["windmills canal row", "polder drainage", "dutch water mill"]},
    {"id": "kings-day-flea-market", "text": "On King's Day anyone can sell second hand goods on the street tax free, a nationwide flea market.", "footage_queries": ["street market netherlands", "flea market crowd", "kings day amsterdam"]},
    {"id": "dutch-design", "text": "'Dutch Design' is a global byword for clever, minimalist and playful product design.", "footage_queries": ["modern design studio", "minimalist furniture", "design exhibition"]},
    {"id": "windmills-unesco", "text": "Dutch windmills are so iconic that several mill complexes hold UNESCO World Heritage status.", "footage_queries": ["windmill heritage site", "dutch windmills landscape", "windmill reflection water"]},
    {"id": "amsterdam-bridges", "text": "Amsterdam has more bridges than Venice, with over 1,500 crossing its canals.", "footage_queries": ["amsterdam bridge canal", "canal bridges aerial", "bridge bikes amsterdam"]},
    {"id": "cycling-infrastructure", "text": "Dutch traffic lights, tunnels and roundabouts are often designed for bikes first, cars second.", "footage_queries": ["bike roundabout netherlands", "cycle lane traffic", "bicycle traffic light"]},
    {"id": "rotterdam-architecture", "text": "Rebuilt after WWII, Rotterdam is now famous for bold modern architecture like the Cube Houses.", "footage_queries": ["rotterdam cube houses", "modern architecture city", "rotterdam skyline"]},
    {"id": "lowest-point-village", "text": "The lowest point in the Netherlands sits nearly seven metres below sea level near Rotterdam.", "footage_queries": ["polder below sea level", "netherlands flat fields", "drainage canal"]},
    {"id": "dutch-liquorice", "text": "The Dutch love salty liquorice ('drop') so much they eat more of it than anyone else.", "footage_queries": ["liquorice candy", "dutch candy shop", "black candy sweets"]},
    {"id": "windmill-coffee", "text": "Many Dutch windmills still grind spices, mustard or cocoa using centuries-old machinery.", "footage_queries": ["windmill grinding stone", "dutch mill interior", "windmill machinery"]},
    {"id": "ijsselmeer-lake", "text": "The IJsselmeer, once an arm of the sea, is now one of Western Europe's largest freshwater lakes.", "footage_queries": ["ijsselmeer lake netherlands", "sailing boat lake", "dutch lake horizon"]},
    {"id": "biking-rain", "text": "The Dutch cycle through rain, wind and snow, and bad weather rarely stops a Dutch commuter.", "footage_queries": ["cycling in rain", "cyclist umbrella city", "rainy street bikes"]},
    {"id": "national-color-orange", "text": "The Dutch national colour is orange, after the royal House of Orange-Nassau.", "footage_queries": ["orange football fans", "netherlands orange crowd", "kings day orange"]},
]


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3")


def _read_json(bucket: str, key: str, default):
    try:
        obj = _s3().get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return default
        raise
    except Exception as e:  # malformed JSON etc. — fall back gracefully
        logger.warning(f"⚠️  Could not read s3://{bucket}/{key}: {e}")
        return default


def _write_json(bucket: str, key: str, data) -> None:
    _s3().put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False),
        ContentType="application/json",
    )


def _load_pool(bucket: str) -> list:
    """Load the fact pool from S3, seeding it from DEFAULT_FACTS on first run."""
    pool = _read_json(bucket, _POOL_KEY, None)
    if not pool:
        try:
            _write_json(bucket, _POOL_KEY, DEFAULT_FACTS)
            logger.info(f"🌱 Seeded fact pool → s3://{bucket}/{_POOL_KEY} ({len(DEFAULT_FACTS)} facts)")
        except Exception as e:
            logger.warning(f"⚠️  Could not seed fact pool to S3: {e}")
        pool = DEFAULT_FACTS
    return pool


# ── Public API ────────────────────────────────────────────────────────────────

def get_fact_for_today(bucket: str) -> dict:
    """Pick today's fact via least-recently-used rotation (state in S3).

    Never-used facts are chosen first; once the whole pool has been shown,
    the oldest-used fact is repeated. Emails a one-time refill reminder when
    the pool is nearly exhausted.
    """
    pool = _load_pool(bucket)
    if not pool:
        raise ValueError("Dutch fact pool is empty")

    rotation = _read_json(bucket, _ROTATION_KEY, {}) or {}
    used = rotation.get("used", {}) or {}
    cycle_alerted = bool(rotation.get("cycle_alerted", False))

    pool_ids = {f["id"] for f in pool}
    used = {k: v for k, v in used.items() if k in pool_ids}  # drop removed facts

    unused = [f for f in pool if f["id"] not in used]
    if unused:
        # Curated pool order → the strongest facts open the calendar.
        fact = unused[0]
    else:
        # All used → least-recently-used (oldest date, pool order as tie-break).
        oldest_date = min(used.values())
        fact = next(f for f in pool if used.get(f["id"]) == oldest_date)

    used[fact["id"]] = date.today().isoformat()
    remaining_unused = sum(1 for f in pool if f["id"] not in used)

    # One-time refill reminder per cycle; re-armed once fresh facts are added.
    if remaining_unused < _REFILL_THRESHOLD and not cycle_alerted:
        try:
            send_alert(
                "Dutch fact pool almost cycled, add new facts",
                f"Only {remaining_unused} unused Dutch facts remain (pool size {len(pool)}).\n"
                f"Facts will start repeating (~{len(pool)}-day cycle).\n"
                f"Add new entries to s3://{bucket}/{_POOL_KEY}, no redeploy needed.",
                "GENERAL",
            )
        except Exception as e:
            logger.warning(f"⚠️  Refill alert failed: {e}")
        cycle_alerted = True
    elif remaining_unused >= _REFILL_THRESHOLD:
        cycle_alerted = False

    try:
        _write_json(bucket, _ROTATION_KEY, {"used": used, "cycle_alerted": cycle_alerted})
    except Exception as e:
        logger.warning(f"⚠️  Could not persist fact rotation: {e}")

    logger.info(
        f"📅 Today's Dutch fact: [{fact['id']}] {fact['text'][:60]}... "
        f"({remaining_unused} unused of {len(pool)} left)"
    )
    return fact
