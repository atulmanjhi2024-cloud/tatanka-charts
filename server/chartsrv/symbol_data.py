"""Symbols the app knows without asking anyone: futures roots, indices, currency pairs, the main cryptocurrencies and
popular FRED series. symbols.py adds every US-listed stock and ETF (Nasdaq Trader's daily files) and more crypto
(Coinbase), and keeps what online searches find."""

# root -> (name, exchange). Watchlist symbol "ES1!" (TradingView's continuous front month).
FUTURES = {
    "ES": ("E-mini S&P 500", "CME"), "MES": ("Micro E-mini S&P 500", "CME"),
    "NQ": ("E-mini Nasdaq-100", "CME"), "MNQ": ("Micro E-mini Nasdaq-100", "CME"),
    "RTY": ("E-mini Russell 2000", "CME"), "M2K": ("Micro E-mini Russell 2000", "CME"),
    "YM": ("E-mini Dow ($5)", "CBOT"), "MYM": ("Micro E-mini Dow", "CBOT"),
    "EMD": ("E-mini S&P MidCap 400", "CME"), "NKD": ("Nikkei 225 (USD)", "CME"),
    "CL": ("Crude Oil WTI", "NYMEX"), "MCL": ("Micro WTI Crude Oil", "NYMEX"), "QM": ("E-mini Crude Oil", "NYMEX"),
    "BZ": ("Brent Crude Oil (last day)", "NYMEX"), "NG": ("Henry Hub Natural Gas", "NYMEX"),
    "QG": ("E-mini Natural Gas", "NYMEX"), "RB": ("RBOB Gasoline", "NYMEX"), "HO": ("NY Harbor ULSD (Heating Oil)", "NYMEX"),
    "GC": ("Gold", "COMEX"), "MGC": ("Micro Gold", "COMEX"), "SI": ("Silver", "COMEX"), "SIL": ("Micro Silver", "COMEX"),
    "HG": ("Copper", "COMEX"), "MHG": ("Micro Copper", "COMEX"), "PL": ("Platinum", "NYMEX"), "PA": ("Palladium", "NYMEX"),
    "ZT": ("2-Year T-Note", "CBOT"), "ZF": ("5-Year T-Note", "CBOT"), "ZN": ("10-Year T-Note", "CBOT"),
    "TN": ("Ultra 10-Year T-Note", "CBOT"), "ZB": ("U.S. Treasury Bond", "CBOT"), "UB": ("Ultra U.S. Treasury Bond", "CBOT"),
    "SR3": ("Three-Month SOFR", "CME"), "ZQ": ("30-Day Federal Funds", "CBOT"),
    "ZC": ("Corn", "CBOT"), "ZS": ("Soybeans", "CBOT"), "ZW": ("Chicago SRW Wheat", "CBOT"), "KE": ("KC HRW Wheat", "CBOT"),
    "ZL": ("Soybean Oil", "CBOT"), "ZM": ("Soybean Meal", "CBOT"), "ZO": ("Oats", "CBOT"), "ZR": ("Rough Rice", "CBOT"),
    "LE": ("Live Cattle", "CME"), "HE": ("Lean Hogs", "CME"), "GF": ("Feeder Cattle", "CME"),
    "6E": ("Euro FX", "CME"), "6J": ("Japanese Yen", "CME"), "6B": ("British Pound", "CME"), "6A": ("Australian Dollar", "CME"),
    "6C": ("Canadian Dollar", "CME"), "6S": ("Swiss Franc", "CME"), "6M": ("Mexican Peso", "CME"),
    "6N": ("New Zealand Dollar", "CME"), "M6E": ("Micro EUR/USD", "CME"),
    "BTC": ("Bitcoin", "CME"), "MBT": ("Micro Bitcoin", "CME"), "ETH": ("Ether", "CME"), "MET": ("Micro Ether", "CME"),
    "VX": ("Cboe VIX Futures", "CFE"),
    "CT": ("Cotton No. 2", "ICE US"), "KC": ("Coffee C", "ICE US"), "SB": ("Sugar No. 11", "ICE US"),
    "CC": ("Cocoa", "ICE US"), "OJ": ("Orange Juice", "ICE US"), "DX": ("U.S. Dollar Index", "ICE US"),
    "FDAX": ("DAX", "EUREX"), "FESX": ("Euro Stoxx 50", "EUREX"), "FGBL": ("Euro-Bund", "EUREX"),
}

# symbol -> (name, Yahoo ticker). TradingView's names for them.
INDICES = {
    "SPX": ("S&P 500", "^GSPC"), "NDX": ("Nasdaq-100", "^NDX"), "IXIC": ("Nasdaq Composite", "^IXIC"),
    "DJI": ("Dow Jones Industrial Average", "^DJI"), "RUT": ("Russell 2000", "^RUT"), "NYA": ("NYSE Composite", "^NYA"),
    "SOX": ("PHLX Semiconductor", "^SOX"), "VIX": ("Cboe Volatility Index", "^VIX"), "VVIX": ("Cboe VIX of VIX", "^VVIX"),
    "VIX9D": ("Cboe 9-Day Volatility", "^VIX9D"), "VIX3M": ("Cboe 3-Month Volatility", "^VIX3M"),
    "SKEW": ("Cboe SKEW Index", "^SKEW"), "MOVE": ("ICE BofAML MOVE Index", "^MOVE"), "DXY": ("U.S. Dollar Index", "DX-Y.NYB"),
    "TNX": ("Cboe 10-Year Treasury Yield", "^TNX"), "TYX": ("Cboe 30-Year Treasury Yield", "^TYX"),
    "FVX": ("Cboe 5-Year Treasury Yield", "^FVX"), "IRX": ("Cboe 13-Week Treasury Bill Yield", "^IRX"),
    "US10Y": ("U.S. 10-Year Yield", "^TNX"), "US30Y": ("U.S. 30-Year Yield", "^TYX"), "US05Y": ("U.S. 5-Year Yield", "^FVX"),
    "HSI": ("Hang Seng", "^HSI"), "NI225": ("Nikkei 225", "^N225"), "KOSPI": ("KOSPI Composite", "^KS11"),
    "NIFTY": ("Nifty 50", "^NSEI"), "SX5E": ("Euro Stoxx 50", "^STOXX50E"), "UKX": ("FTSE 100", "^FTSE"),
    "DE40": ("DAX", "^GDAXI"), "PX1": ("CAC 40", "^FCHI"), "TSX": ("S&P/TSX Composite", "^GSPTSE"),
}

CURRENCIES = {
    "USD": "U.S. Dollar", "EUR": "Euro", "GBP": "British Pound", "JPY": "Japanese Yen", "CHF": "Swiss Franc",
    "CAD": "Canadian Dollar", "AUD": "Australian Dollar", "NZD": "New Zealand Dollar", "CNH": "Chinese Yuan (offshore)",
    "HKD": "Hong Kong Dollar", "SEK": "Swedish Krona", "NOK": "Norwegian Krone", "MXN": "Mexican Peso",
    "SGD": "Singapore Dollar", "ZAR": "South African Rand", "TRY": "Turkish Lira", "INR": "Indian Rupee",
    "KRW": "South Korean Won", "BRL": "Brazilian Real", "DKK": "Danish Krone", "PLN": "Polish Zloty",
}
MAJORS = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]   # the market's order: EURUSD, USDJPY, GBPJPY


def fx_pairs() -> list[str]:
    """Every pair of the majors, quoted the usual way round, and the dollar against the rest."""
    out = [a + b for i, a in enumerate(MAJORS) for b in MAJORS[i + 1:]]
    return out + ["USD" + c for c in CURRENCIES if c not in MAJORS]


CRYPTO = {   # by market value, biggest first
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "BNB": "BNB", "DOGE": "Dogecoin",
    "ADA": "Cardano", "TRX": "TRON", "AVAX": "Avalanche", "LINK": "Chainlink", "DOT": "Polkadot", "LTC": "Litecoin",
    "BCH": "Bitcoin Cash", "XLM": "Stellar", "SHIB": "Shiba Inu", "ETC": "Ethereum Classic", "FIL": "Filecoin",
    "AAVE": "Aave", "HBAR": "Hedera", "ATOM": "Cosmos", "NEAR": "NEAR Protocol", "ALGO": "Algorand",
}

# Popular FRED series: id -> (title, frequency D/W/M/Q/A). Any other FRED id works too ("FRED:XXXX"), and with a free
# FRED API key the search finds all of them.
FRED = {
    "DFF": ("Federal Funds Effective Rate", "D"), "FEDFUNDS": ("Federal Funds Effective Rate", "M"),
    "DFEDTARU": ("Federal Funds Target Range - Upper Limit", "D"), "DFEDTARL": ("Federal Funds Target Range - Lower Limit", "D"),
    "SOFR": ("Secured Overnight Financing Rate", "D"), "IORB": ("Interest Rate on Reserve Balances", "D"),
    "DPRIME": ("Bank Prime Loan Rate", "D"), "ECBDFR": ("ECB Deposit Facility Rate", "D"),
    "MORTGAGE30US": ("30-Year Fixed Rate Mortgage Average", "W"), "MORTGAGE15US": ("15-Year Fixed Rate Mortgage Average", "W"),
    "DTB3": ("3-Month Treasury Bill Secondary Market Rate", "D"),
    "DGS1MO": ("1-Month Treasury Constant Maturity Rate", "D"), "DGS3MO": ("3-Month Treasury Constant Maturity Rate", "D"),
    "DGS6MO": ("6-Month Treasury Constant Maturity Rate", "D"), "DGS1": ("1-Year Treasury Constant Maturity Rate", "D"),
    "DGS2": ("2-Year Treasury Constant Maturity Rate", "D"), "DGS3": ("3-Year Treasury Constant Maturity Rate", "D"),
    "DGS5": ("5-Year Treasury Constant Maturity Rate", "D"), "DGS7": ("7-Year Treasury Constant Maturity Rate", "D"),
    "DGS10": ("10-Year Treasury Constant Maturity Rate", "D"), "DGS20": ("20-Year Treasury Constant Maturity Rate", "D"),
    "DGS30": ("30-Year Treasury Constant Maturity Rate", "D"),
    "T10Y2Y": ("10-Year Minus 2-Year Treasury Yield Spread", "D"), "T10Y3M": ("10-Year Minus 3-Month Treasury Yield Spread", "D"),
    "T5YIE": ("5-Year Breakeven Inflation Rate", "D"), "T10YIE": ("10-Year Breakeven Inflation Rate", "D"),
    "T5YIFR": ("5-Year, 5-Year Forward Inflation Expectation Rate", "D"),
    "DFII10": ("10-Year Treasury Inflation-Indexed (TIPS) Yield", "D"),
    "BAMLH0A0HYM2": ("ICE BofA US High Yield Index Option-Adjusted Spread", "D"),
    "BAMLH0A3HYC": ("ICE BofA CCC & Lower US High Yield Option-Adjusted Spread", "D"),
    "BAMLC0A0CM": ("ICE BofA US Corporate Index Option-Adjusted Spread", "D"),
    "BAA10Y": ("Moody's Baa Corporate Yield Minus 10-Year Treasury", "D"),
    "AAA10Y": ("Moody's Aaa Corporate Yield Minus 10-Year Treasury", "D"),
    "CPIAUCSL": ("Consumer Price Index (CPI), All Items", "M"), "CPILFESL": ("Core CPI (Less Food and Energy)", "M"),
    "PCEPI": ("PCE Price Index", "M"), "PCEPILFE": ("Core PCE Price Index", "M"),
    "PPIFIS": ("Producer Price Index: Final Demand", "M"), "MICH": ("University of Michigan: Inflation Expectation", "M"),
    "UNRATE": ("Unemployment Rate", "M"), "U6RATE": ("U-6 Unemployment Rate", "M"),
    "PAYEMS": ("All Employees, Total Nonfarm (Payrolls)", "M"), "ICSA": ("Initial Jobless Claims", "W"),
    "CCSA": ("Continued Jobless Claims", "W"), "CIVPART": ("Labor Force Participation Rate", "M"),
    "JTSJOL": ("Job Openings: Total Nonfarm (JOLTS)", "M"),
    "CES0500000003": ("Average Hourly Earnings, Total Private", "M"),
    "SAHMREALTIME": ("Real-time Sahm Rule Recession Indicator", "M"),
    "GDP": ("Gross Domestic Product", "Q"), "GDPC1": ("Real Gross Domestic Product", "Q"),
    "A191RL1Q225SBEA": ("Real GDP Growth (Percent Change, Annual Rate)", "Q"),
    "INDPRO": ("Industrial Production: Total Index", "M"), "TCU": ("Capacity Utilization: Total Index", "M"),
    "RSAFS": ("Advance Retail Sales: Retail Trade and Food Services", "M"),
    "UMCSENT": ("University of Michigan: Consumer Sentiment", "M"),
    "HOUST": ("Housing Starts: Total Units", "M"), "PERMIT": ("Building Permits: Total Units", "M"),
    "CSUSHPINSA": ("S&P CoreLogic Case-Shiller U.S. National Home Price Index", "M"),
    "EXHOSLUSM495S": ("Existing Home Sales", "M"), "MSPUS": ("Median Sales Price of Houses Sold", "Q"),
    "DGORDER": ("Manufacturers' New Orders: Durable Goods", "M"), "TOTALSA": ("Total Vehicle Sales", "M"),
    "PSAVERT": ("Personal Saving Rate", "M"), "PCE": ("Personal Consumption Expenditures", "M"),
    "TOTALSL": ("Total Consumer Credit", "M"), "DRCCLACBS": ("Delinquency Rate on Credit Card Loans", "Q"),
    "M2SL": ("M2 Money Stock", "M"), "WM2NS": ("M2 Money Stock (Weekly)", "W"), "BOGMBASE": ("Monetary Base", "M"),
    "WALCL": ("Federal Reserve Total Assets", "W"), "RRPONTSYD": ("Overnight Reverse Repurchase Agreements (Fed)", "D"),
    "WTREGEN": ("Treasury General Account", "W"), "WRESBAL": ("Reserve Balances with Federal Reserve Banks", "W"),
    "SP500": ("S&P 500", "D"), "NASDAQCOM": ("NASDAQ Composite Index", "D"), "DJIA": ("Dow Jones Industrial Average", "D"),
    "VIXCLS": ("CBOE Volatility Index: VIX", "D"), "DTWEXBGS": ("Nominal Broad U.S. Dollar Index", "D"),
    "DEXUSEU": ("U.S. Dollars to Euro Exchange Rate", "D"), "DEXJPUS": ("Japanese Yen to U.S. Dollar Exchange Rate", "D"),
    "DEXCHUS": ("Chinese Yuan to U.S. Dollar Exchange Rate", "D"), "DEXUSUK": ("U.S. Dollars to Pound Sterling Exchange Rate", "D"),
    "DEXCAUS": ("Canadian Dollars to U.S. Dollar Exchange Rate", "D"),
    "DCOILWTICO": ("Crude Oil Prices: WTI - Cushing, Oklahoma", "D"), "DCOILBRENTEU": ("Crude Oil Prices: Brent - Europe", "D"),
    "DHHNGSP": ("Henry Hub Natural Gas Spot Price", "D"), "GASREGW": ("US Regular Gasoline Price", "W"),
    "NFCI": ("Chicago Fed National Financial Conditions Index", "W"), "STLFSI4": ("St. Louis Fed Financial Stress Index", "W"),
    "USREC": ("NBER Recession Indicator", "M"), "RECPROUSM156N": ("Smoothed U.S. Recession Probabilities", "M"),
    "GFDEBTN": ("Federal Debt: Total Public Debt", "Q"), "GFDEGDQ188S": ("Federal Debt as Percent of GDP", "Q"),
    "FYFSD": ("Federal Surplus or Deficit", "A"),
}
# EIA's weekly petroleum and natural gas series: id -> (title, frequency). Needs a free EIA API key.
EIA = {
    "PET.WCESTUS1.W": ("U.S. Crude Oil Stocks excluding SPR (EIA)", "W"),
    "PET.W_EPC0_SAX_YCUOK_MBBL.W": ("Cushing, OK Crude Oil Stocks (EIA)", "W"),
    "PET.WCSSTUS1.W": ("U.S. Crude Oil in the SPR (EIA)", "W"),
    "PET.WGTSTUS1.W": ("U.S. Total Gasoline Stocks (EIA)", "W"),
    "PET.WDISTUS1.W": ("U.S. Distillate Fuel Oil Stocks (EIA)", "W"),
    "PET.WCRFPUS2.W": ("U.S. Crude Oil Field Production (EIA)", "W"),
    "PET.WPULEUS3.W": ("U.S. Refinery Utilization (EIA)", "W"),
    "PET.WCRIMUS2.W": ("U.S. Crude Oil Imports (EIA)", "W"),
    "PET.WCREXUS2.W": ("U.S. Crude Oil Exports (EIA)", "W"),
    "PET.WRPUPUS2.W": ("U.S. Product Supplied of Petroleum Products (EIA)", "W"),
    "NG.NW2_EPG0_SWO_R48_BCF.W": ("Lower 48 Natural Gas Working Storage (EIA)", "W"),
}
FREQ = {"D": "Daily", "W": "Weekly", "BW": "Biweekly", "M": "Monthly", "Q": "Quarterly", "SA": "Semiannual", "A": "Annual"}
