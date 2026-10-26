"""IMF real exchange rate explorer — Python 3.12 / Streamlit."""
import io
import re
from datetime import date

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pycountry
import requests
import streamlit as st

BASE = "https://api.imf.org/external/sdmx/3.0"
FIRST = pd.Period("1976-01", freq="M")
LAST = pd.Period(date.today().strftime("%Y-%m"), freq="M")

st.set_page_config(page_title="IMF Real Exchange Rate Explorer", page_icon="🌍", layout="wide")
st.title("🌍 IMF Real Exchange Rate Explorer")
st.caption("Monthly CPI and nominal exchange rates from the International Monetary Fund (IMF)")

# IMF country dimension generally uses ISO 3166-1 alpha-3, plus G163 for euro area.
COUNTRIES = {c.alpha_3: c.name for c in pycountry.countries}
COUNTRIES["G163"] = "Euro area"
COUNTRIES = dict(sorted(COUNTRIES.items(), key=lambda x: x[1]))
LABELS = {code: f"{name} ({code})" for code, name in COUNTRIES.items()}


def parse_month(value):
    """Accept IMF monthly periods such as 2024-M01, 2024-01 or 2024-01-01."""
    s = str(value).strip()
    m = re.match(r"^(\d{4})-M?(\d{1,2})(?:-\d{2})?$", s)
    if not m:
        return pd.NaT
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return pd.NaT
    return pd.Timestamp(year, month, 1)


@st.cache_data(ttl=12 * 60 * 60, show_spinner=False)
def get_imf_series(code, kind):
    """Return a monthly Series. Do not silently replace failed data with invented data."""
    if kind == "fx" and code == "USA":
        # By definition: U.S. dollars per one U.S. dollar = 1.
        idx = pd.date_range(FIRST.start_time, LAST.start_time, freq="MS")
        return pd.Series(1.0, index=idx, name="value")

    if kind == "cpi":
        flow, key = "CPI", f"{code}.CPI._T.IX.M"
    else:
        flow, key = "ER", f"{code}.XDC_USD.PA_RT.M"

    url = f"{BASE}/data/dataflow/IMF.STA/{flow}/~/{key}"
    errors = []
    # The IMF supports SDMX-CSV on its current API. Some gateways accept
    # the temporal constraint instead of startPeriod.
    for params in (
        {"c[TIME_PERIOD]": "ge:1976-M01"},
        {"startPeriod": "1976-01"},
    ):
        try:
            response = requests.get(url, params=params, headers={"Accept": "text/csv"}, timeout=45)
            response.raise_for_status()
            if not response.content or not response.text.strip():
                errors.append("empty response")
                continue
            df = pd.read_csv(io.StringIO(response.text), low_memory=False)
            df.columns = [str(col).strip().upper() for col in df.columns]
            if not {"TIME_PERIOD", "OBS_VALUE"}.issubset(df.columns):
                errors.append(f"unexpected columns: {list(df.columns)[:12]}")
                continue
            if "COUNTRY" in df.columns:
                df = df[df["COUNTRY"].astype(str).str.upper() == code]
            df["month"] = df["TIME_PERIOD"].map(parse_month)
            df["value"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
            df = df.dropna(subset=["month", "value"])
            df = df[(df["month"] >= FIRST.start_time) & (df["month"] <= LAST.start_time)]
            if df.empty:
                errors.append("no usable monthly observations")
                continue
            return df.drop_duplicates("month", keep="last").set_index("month")["value"].sort_index()
        except (requests.RequestException, ValueError, pd.errors.ParserError) as ex:
            errors.append(str(ex))
    raise RuntimeError(f"IMF {flow} series {key} unavailable: {'; '.join(errors)}")


def label_currency(code):
    if code == "G163":
        return "EUR"
    # Country-to-currency mappings are only used for readable labels.
    return {"USA": "USD", "GBR": "GBP", "JPN": "JPY", "CAN": "CAD", "AUS": "AUD", "CHE": "CHF", "CHN": "CNY", "IND": "INR", "DEU": "EUR", "FRA": "EUR", "ITA": "EUR", "ESP": "EUR"}.get(code, "local currency")


with st.sidebar:
    st.header("Choose your comparison")
    choices = list(COUNTRIES)
    code1 = st.selectbox("Country 1", choices, index=choices.index("GBR"), format_func=lambda c: LABELS[c])
    code2 = st.selectbox("Country 2", choices, index=choices.index("USA"), format_func=lambda c: LABELS[c])
    start = st.date_input("Start month", value=date(2000, 1, 1), min_value=FIRST.start_time.date(), max_value=LAST.start_time.date())
    end = st.date_input("End month", value=LAST.start_time.date(), min_value=FIRST.start_time.date(), max_value=LAST.start_time.date())
    mode = st.radio("Display", ["Real", "Nominal", "Both"], horizontal=True, index=2)

start_month, end_month = pd.Period(start, freq="M"), pd.Period(end, freq="M")
if code1 == code2:
    st.info("Select two different countries to compare.")
    st.stop()
if start_month > end_month:
    st.error("Start month must not be later than end month.")
    st.stop()

st.markdown("**Definition:** S(t) = Country 2 currency units per one Country 1 currency unit; "
            "RER(t) = S(t) × P₁(t) / P₂(t). "
            "Real Index(t) = 100 × RER(t) / mean(RER) within the selected valid sample.")

try:
    with st.spinner("Retrieving monthly IMF series…"):
        p1 = get_imf_series(code1, "cpi").rename("P1_CPI")
        p2 = get_imf_series(code2, "cpi").rename("P2_CPI")
        fx1 = get_imf_series(code1, "fx").rename("FX1_local_per_USD")
        fx2 = get_imf_series(code2, "fx").rename("FX2_local_per_USD")
except RuntimeError as e:
    st.error("IMF download failed or one of the selected series has no data. Try another pair or retry later.")
    st.code(str(e))
    st.info("Live IMF requests can be temporarily unavailable. The app does not substitute simulated observations.")
    st.stop()

# IMF FX quotes: Country 1 currency / USD and Country 2 currency / USD.
# Therefore S = FX2 / FX1, quoted as Country 2 currency per Country 1 currency.
df = pd.concat([p1, p2, fx1, fx2], axis=1).sort_index()
df = df.loc[(df.index >= start_month.start_time) & (df.index <= end_month.start_time)].copy()
for name in ["P1_CPI", "P2_CPI", "FX1_local_per_USD", "FX2_local_per_USD"]:
    df.loc[df[name] <= 0, name] = float("nan")
df["S_country2_per_country1"] = df["FX2_local_per_USD"] / df["FX1_local_per_USD"]
df["RER"] = df["S_country2_per_country1"] * df["P1_CPI"] / df["P2_CPI"]
valid = df["RER"].dropna()
if valid.empty:
    st.warning("No months have all four positive observations in this date range. Try different dates or countries.")
    st.stop()
rer_star = valid.mean()
df["RER_sample_mean"] = rer_star
df["Real_Index"] = 100.0 * df["RER"] / rer_star
# Save only complete monthly records so CSV averages and spot checks are unambiguous.
result = df.dropna(subset=["P1_CPI", "P2_CPI", "FX1_local_per_USD", "FX2_local_per_USD", "RER", "Real_Index"]).copy()
result.index.name = "Month"
result = result.reset_index()
result["Month"] = result["Month"].dt.strftime("%Y-%m")

unit1, unit2 = label_currency(code1), label_currency(code2)
quoted = f"{unit2} per {unit1}" if "local currency" not in (unit1, unit2) else "Country 2 currency per Country 1 currency"
c1, c2, c3 = st.columns(3)
c1.metric("Complete months", f"{len(result):,}")
c2.metric("Sample mean RER*", f"{rer_star:.5g}")
c3.metric("Mean Real Index", f"{result['Real_Index'].mean():.6f}")
st.caption(f"Nominal direction: **{quoted}**. Complete-observation coverage: {result['Month'].iloc[0]} to {result['Month'].iloc[-1]}.")

x = pd.to_datetime(result["Month"])
if mode == "Both":
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=x, y=result["Real_Index"], name="Real Index (mean = 100)", mode="lines"), secondary_y=False)
    fig.add_trace(go.Scatter(x=x, y=result["S_country2_per_country1"], name=f"Nominal S ({quoted})", mode="lines"), secondary_y=True)
    fig.update_yaxes(title_text="Real index", secondary_y=False)
    fig.update_yaxes(title_text=f"Nominal S ({quoted})", secondary_y=True)
else:
    column, title = ("Real_Index", "Real index (mean = 100)") if mode == "Real" else ("S_country2_per_country1", f"Nominal S ({quoted})")
    fig = go.Figure(go.Scatter(x=x, y=result[column], mode="lines", name=title))
    fig.update_yaxes(title_text=title)
fig.update_layout(title=f"{COUNTRIES[code1]} vs. {COUNTRIES[code2]}", xaxis_title="Month", hovermode="x unified", height=530, legend=dict(orientation="h", y=1.08), margin=dict(l=20, r=20, t=70, b=20))
st.plotly_chart(fig, use_container_width=True)
st.download_button("⬇️ Download CSV of selected sample", data=result.to_csv(index=False).encode("utf-8"), file_name=f"real_exchange_rate_{code1}_{code2}_{start_month}_{end_month}.csv", mime="text/csv")
with st.expander("Show data and calculation details"):
    st.dataframe(result, use_container_width=True, hide_index=True)
    st.markdown("**Manual check:** Take any CSV row: `S = FX2_local_per_USD / FX1_local_per_USD`; "
                "`RER = S × P1_CPI / P2_CPI`; `Real_Index = 100 × RER / RER_sample_mean`. "
                "The real-index sample average must be 100 (apart from floating-point rounding).")
st.caption("Source: IMF Statistics Department SDMX 3.0 API, CPI and ER dataflows. Availability varies by country and series; data are retrieved live and cached for 12 hours.")
