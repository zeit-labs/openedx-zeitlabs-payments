"""
ISO 4217 currency data and other static constants.

Kept separate from helpers to avoid pulling the helpers module's
edx-platform dependencies whenever a constants-only consumer (e.g. a
management command or a test) only needs the currency table.
"""
import re
from datetime import timedelta

# Shape of every money column in this package. Kept here so a value can be refused before the database sees
# it, rather than being truncated or rejected differently by SQLite and MySQL.
MONEY_MAX_DIGITS = 12
MONEY_DECIMAL_PLACES = 3

# Card-number redaction. Both chokepoints (AuditLog.log and handle_payment) read these to decide
# whether a digit run is a card number worth masking. A false positive is worse than a miss:
# masking an amount corrupts reconciliation, while the gateways already mask.

# A candidate card number: 13-19 digits, optionally grouped with single spaces or dashes. Dots
# are deliberately not separators, so a decimal amount like "999999999999.000" never becomes a
# candidate. The regex is the whole length rule: digit runs shorter than 13 (order ids,
# timestamps) or longer than 19 never match.
_CANDIDATE_RE = re.compile(r'\d(?:[\s-]?\d){12,18}')

# The major industry identifiers used by the card schemes this plugin can meet (Visa 4,
# Mastercard 2/5, Amex 3, Discover 6). National schemes under 9 are out of scope: refusing them
# here avoids masking order ids and amounts that merely happen to be 13-19 digits long.
_CARD_BRAND_MII = '23456'

_SEPARATORS_RE = re.compile(r'[\s-]+')

# The only schemes a payment origin may name. Receipts are addressed on it, and a receipt is a web page a learner
# opens; anything else in the column would be a QR code pointing at something a phone camera cannot follow.
_PAYMENT_ORIGIN_SCHEMES = frozenset({'http', 'https'})

# Invoice-series name. An RFC 1123 host label in its capitalized form: letters, digits and inner hyphens,
# beginning and ending with a letter or digit. An inner hyphen is safe because an invoice number is split on its
# last hyphen to recover the series it belongs to; one on either end would leave the series named by nothing.
_INVOICE_PREFIX_RE = re.compile(r'[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?')
_INVOICE_PREFIX_MAX_LENGTH = 63

# ISO 4217 minor-unit table (M3, M11).
# Maps ISO 4217 alphabetic currency code to its exponent (number of decimal places).
# Excludes:
#   - 'CLF', 'UYW': units of account (CLF, Unidad Previsional) -- never payable.
#   - 'XAU', 'XAG', 'XPT', 'XPD': precious metals.
#   - 'XDR', 'XTS', 'XXX', 'XSU', 'XBA'..'XBD', 'XAD': non-currency / fund / bond codes.
#   - 'MGA', 'MRU': base-5 currencies, not supported.
# An unsupported currency code raises; "not payable" codes do not appear here on purpose.
ISO_4217_CURRENCY_EXPONENTS: dict[str, int] = {
    'AED': 2, 'AFN': 2, 'ALL': 2, 'AMD': 2, 'ANG': 2, 'AOA': 2, 'ARS': 2,
    'AUD': 2, 'AWG': 2, 'AZN': 2, 'BAM': 2, 'BBD': 2, 'BDT': 2, 'BGN': 2,
    'BHD': 3, 'BIF': 0, 'BMD': 2, 'BND': 2, 'BOB': 2, 'BOV': 2, 'BRL': 2,
    'BSD': 2, 'BTN': 2, 'BWP': 2, 'BYN': 2, 'BZD': 2, 'CAD': 2, 'CDF': 2,
    'CHE': 2, 'CHF': 2, 'CHW': 2, 'CLP': 0, 'CNY': 2, 'COP': 2, 'COU': 2,
    'CRC': 2, 'CUP': 2, 'CVE': 2, 'CZK': 2, 'DJF': 0, 'DKK': 2, 'DOP': 2,
    'DZD': 2, 'EGP': 2, 'ERN': 2, 'ETB': 2, 'EUR': 2, 'FJD': 2, 'FKP': 2,
    'GBP': 2, 'GEL': 2, 'GHS': 2, 'GIP': 2, 'GMD': 2, 'GNF': 0, 'GTQ': 2,
    'GYD': 2, 'HKD': 2, 'HNL': 2, 'HTG': 2, 'HUF': 2, 'IDR': 2, 'ILS': 2,
    'INR': 2, 'IQD': 3, 'IRR': 2, 'ISK': 0, 'JMD': 2, 'JOD': 3, 'JPY': 0,
    'KES': 2, 'KGS': 2, 'KHR': 2, 'KMF': 0, 'KPW': 2, 'KRW': 0, 'KWD': 3,
    'KYD': 2, 'KZT': 2, 'LAK': 2, 'LBP': 2, 'LKR': 2, 'LRD': 2, 'LSL': 2,
    'LYD': 3, 'MAD': 2, 'MDL': 2, 'MKD': 2, 'MMK': 2, 'MNT': 2, 'MOP': 2,
    'MUR': 2, 'MVR': 2, 'MWK': 2, 'MXN': 2, 'MXV': 2, 'MYR': 2, 'MZN': 2,
    'NAD': 2, 'NGN': 2, 'NIO': 2, 'NOK': 2, 'NPR': 2, 'NZD': 2, 'OMR': 3,
    'PAB': 2, 'PEN': 2, 'PGK': 2, 'PHP': 2, 'PKR': 2, 'PLN': 2, 'PYG': 0,
    'QAR': 2, 'RON': 2, 'RSD': 2, 'RUB': 2, 'RWF': 0, 'SAR': 2, 'SBD': 2,
    'SCR': 2, 'SDG': 2, 'SEK': 2, 'SGD': 2, 'SHP': 2, 'SLE': 2, 'SOS': 2,
    'SRD': 2, 'SSP': 2, 'STN': 2, 'SVC': 2, 'SYP': 2, 'SZL': 2, 'THB': 2,
    'TJS': 2, 'TMT': 2, 'TND': 3, 'TOP': 2, 'TRY': 2, 'TTD': 2, 'TWD': 2,
    'TZS': 2, 'UAH': 2, 'UGX': 0, 'USD': 2, 'USN': 2, 'UYI': 0, 'UYU': 2,
    'UZS': 2, 'VED': 2, 'VES': 2, 'VND': 0, 'VUV': 0, 'WST': 2, 'XAF': 0,
    'XCD': 2, 'XCG': 2, 'XOF': 0, 'XPF': 0, 'YER': 2, 'ZAR': 2, 'ZMW': 2,
    'ZWG': 2,
}

# ISO 4217 active register: numeric code -> alpha code.
# Maps the gateway's three-digit numeric currency code to its ISO 4217 alphabetic
# equivalent so BaseProcessor.map_currency can translate a processor's response
# without per-processor knowledge of the dialect. Numeric codes that are not in this
# table (or any value that is not a three-digit numeric code) pass through
# unchanged; the write path validates the result with canonical_currency, and a
# value that is not a supported ISO 4217 alpha code lands on a
# recorded-but-not-reconciled transaction row, never on a settled one.
#
# The register mirrors ISO_4217_CURRENCY_EXPONENTS: no entry translates into a
# currency the plugin cannot settle. Units of account (CLF, UYW) and base-5
# currencies (MGA, MRU) are absent here for the same reason they are absent from
# the exponents table, and a translation must never produce a value that
# canonical_currency would then refuse. ANG (Netherlands Antillean guilder,
# withdrawn) is not included either -- 532 now means XCG (Caribbean guilder).
# Historical codes such as ATS, FRF, DEM, ITL, BGL, EEK, LVL, LTL, LUF, MTL, ZWD
# and ZWL are omitted on purpose: gateways do not return them and the write path
# would refuse them.
ISO_4217_NUMERIC_TO_ALPHA: dict[str, str] = {
    '008': 'ALL',  # Lek
    '012': 'DZD',  # Algerian dinar
    '032': 'ARS',  # Argentine peso
    '036': 'AUD',  # Australian dollar
    '044': 'BSD',  # Bahamian dollar
    '048': 'BHD',  # Bahraini dinar
    '050': 'BDT',  # Taka
    '051': 'AMD',  # Armenian dram
    '052': 'BBD',  # Barbados dollar
    '060': 'BMD',  # Bermudian dollar
    '064': 'BTN',  # Ngultrum
    '068': 'BOB',  # Boliviano
    '072': 'BWP',  # Pula
    '084': 'BZD',  # Belize dollar
    '090': 'SBD',  # Solomon Islands dollar
    '096': 'BND',  # Brunei dollar
    '104': 'MMK',  # Kyat
    '108': 'BIF',  # Burundian franc
    '116': 'KHR',  # Riel
    '124': 'CAD',  # Canadian dollar
    '132': 'CVE',  # Cape Verdean escudo
    '136': 'KYD',  # Cayman Islands dollar
    '144': 'LKR',  # Sri Lankan rupee
    '152': 'CLP',  # Chilean peso
    '156': 'CNY',  # Yuan Renminbi
    '170': 'COP',  # Colombian peso
    '174': 'KMF',  # Comorian franc
    '188': 'CRC',  # Costa Rican colon
    '192': 'CUP',  # Cuban peso
    '203': 'CZK',  # Czech koruna
    '208': 'DKK',  # Danish krone
    '214': 'DOP',  # Dominican peso
    '222': 'SVC',  # El Salvador colon
    '230': 'ETB',  # Ethiopian birr
    '232': 'ERN',  # Nakfa
    '238': 'FKP',  # Falkland Islands pound
    '242': 'FJD',  # Fiji dollar
    '262': 'DJF',  # Djibouti franc
    '270': 'GMD',  # Dalasi
    '292': 'GIP',  # Gibraltar pound
    '320': 'GTQ',  # Quetzal
    '324': 'GNF',  # Guinean franc
    '328': 'GYD',  # Guyana dollar
    '332': 'HTG',  # Gourde
    '340': 'HNL',  # Lempira
    '344': 'HKD',  # Hong Kong dollar
    '348': 'HUF',  # Forint
    '352': 'ISK',  # Iceland krona
    '356': 'INR',  # Indian rupee
    '360': 'IDR',  # Rupiah
    '364': 'IRR',  # Iranian rial
    '368': 'IQD',  # Iraqi dinar
    '376': 'ILS',  # New Israeli sheqel
    '388': 'JMD',  # Jamaican dollar
    '392': 'JPY',  # Yen
    '398': 'KZT',  # Tenge
    '400': 'JOD',  # Jordanian dinar
    '404': 'KES',  # Kenyan shilling
    '408': 'KPW',  # North Korean won
    '410': 'KRW',  # Won
    '414': 'KWD',  # Kuwaiti dinar
    '417': 'KGS',  # Som
    '418': 'LAK',  # Lao kip
    '422': 'LBP',  # Lebanese pound
    '426': 'LSL',  # Loti
    '430': 'LRD',  # Liberian dollar
    '434': 'LYD',  # Libyan dinar
    '446': 'MOP',  # Pataca
    '454': 'MWK',  # Malawi kwacha
    '458': 'MYR',  # Malaysian ringgit
    '462': 'MVR',  # Rufiyaa
    '480': 'MUR',  # Mauritius rupee
    '484': 'MXN',  # Mexican peso
    '496': 'MNT',  # Tugrik
    '498': 'MDL',  # Moldovan leu
    '504': 'MAD',  # Moroccan dirham
    '512': 'OMR',  # Rial Omani
    '516': 'NAD',  # Namibia dollar
    '524': 'NPR',  # Nepalese rupee
    '532': 'XCG',  # Caribbean guilder (replaced ANG in 2025)
    '533': 'AWG',  # Aruban florin
    '548': 'VUV',  # Vatu
    '554': 'NZD',  # New Zealand dollar
    '558': 'NIO',  # Cordoba oro
    '566': 'NGN',  # Naira
    '578': 'NOK',  # Norwegian krone
    '586': 'PKR',  # Pakistan rupee
    '590': 'PAB',  # Balboa
    '598': 'PGK',  # Kina
    '600': 'PYG',  # Guarani
    '604': 'PEN',  # Sol
    '608': 'PHP',  # Philippine peso
    '634': 'QAR',  # Qatari rial
    '643': 'RUB',  # Russian ruble
    '646': 'RWF',  # Rwanda franc
    '654': 'SHP',  # Saint Helena pound
    '682': 'SAR',  # Saudi riyal
    '690': 'SCR',  # Seychelles rupee
    '702': 'SGD',  # Singapore dollar
    '704': 'VND',  # Dong
    '706': 'SOS',  # Somali shilling
    '710': 'ZAR',  # Rand
    '728': 'SSP',  # South Sudanese pound
    '748': 'SZL',  # Lilangeni
    '752': 'SEK',  # Swedish krona
    '756': 'CHF',  # Swiss franc
    '760': 'SYP',  # Syrian pound
    '764': 'THB',  # Baht
    '776': 'TOP',  # Pa'anga
    '780': 'TTD',  # Trinidad and Tobago dollar
    '784': 'AED',  # UAE dirham
    '788': 'TND',  # Tunisian dinar
    '800': 'UGX',  # Uganda shilling
    '807': 'MKD',  # Denar
    '818': 'EGP',  # Egyptian pound
    '826': 'GBP',  # Pound sterling
    '834': 'TZS',  # Tanzanian shilling
    '840': 'USD',  # US dollar
    '858': 'UYU',  # Peso Uruguayo
    '860': 'UZS',  # Uzbekistan sum
    '882': 'WST',  # Tala
    '886': 'YER',  # Yemeni rial
    '901': 'TWD',  # New Taiwan dollar
    '924': 'ZWG',  # Zimbabwe Gold
    '925': 'SLE',  # Leone
    '926': 'VED',  # Bolivian digital
    '928': 'VES',  # Bolivar Soberano
    '930': 'STN',  # Dobra
    '933': 'BYN',  # Belarusian ruble
    '934': 'TMT',  # Turkmenistan new manat
    '936': 'GHS',  # Ghana cedi
    '938': 'SDG',  # Sudanese pound
    '940': 'UYI',  # Uruguayan peso unidades indexadas
    '941': 'RSD',  # Serbian dinar
    '943': 'MZN',  # Mozambique metical
    '944': 'AZN',  # Azerbaijan manat
    '946': 'RON',  # Romanian leu
    '947': 'CHE',  # WIR Euro
    '948': 'CHW',  # WIR franc
    '949': 'TRY',  # Turkish lira
    '950': 'XAF',  # CFA franc BEAC
    '951': 'XCD',  # East Caribbean dollar
    '952': 'XOF',  # CFA franc BCEAO
    '953': 'XPF',  # CFP franc
    '967': 'ZMW',  # Zambian kwacha
    '968': 'SRD',  # Surinam Dollar
    '970': 'COU',  # Unidad de Valor Real
    '971': 'AFN',  # Afghani
    '972': 'TJS',  # Somoni
    '973': 'AOA',  # Kwanza
    '975': 'BGN',  # Bulgarian lev
    '976': 'CDF',  # Congolese franc
    '977': 'BAM',  # Convertible mark
    '978': 'EUR',  # Euro
    '979': 'MXV',  # Mexican Unidad de Inversion
    '980': 'UAH',  # Hryvnia
    '981': 'GEL',  # Lari
    '984': 'BOV',  # Mvdol
    '985': 'PLN',  # Polish zloty
    '986': 'BRL',  # Brazilian real
    '997': 'USN',  # US dollar (next day)
}

# How long a cart handed off with no order recorded is taken as a hand-off still running. Every processor records its
# order before sending the learner to the gateway, so a cart still without one after this never reached it.
HANDOFF_GRACE_PERIOD = timedelta(minutes=10)

# The same, for a cart handed off before carts recorded their gateway, whose processor cannot be told. A day outlasts
# any gateway's payment session.
UNRECORDED_GATEWAY_GRACE_PERIOD = timedelta(hours=24)
