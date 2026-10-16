import streamlit as st
import yfinance as yf
import plotly.graph_objects as go

# Set page layout to wide
st.set_page_config(page_title="Python Native Watchlist", layout="wide")

st.title("📈 Interactive Stock Watchlist (Native Python / Plotly)")

# Define watchlist
stocks = {
    "Apple (AAPL)": "AAPL",
    "Tesla (TSLA)": "TSLA",
    "Microsoft (MSFT)": "MSFT",
    "NVIDIA (NVDA)": "NVDA",
    "S&P 500 ETF (SPY)": "SPY",
    "Bitcoin (BTC-USD)": "BTC-USD"
}

# Sidebar selection
st.sidebar.header("Navigation")
selected_name = st.sidebar.radio("Select a Stock:", list(stocks.keys()))
ticker = stocks[selected_name]

# Sidebar settings
st.sidebar.divider()
chart_height = st.sidebar.slider("Chart Height (pixels)", min_value=400, max_value=1000, value=750, step=50)

# Fetch historical stock data
@st.cache_data
def load_data(tkr):
    df = yf.download(tkr, period="max", interval="1d")
    # Flatten multi-index columns if returned by newer yfinance versions
    if hasattr(df.columns, 'levels') and len(df.columns.levels) > 1:
        df.columns = df.columns.get_level_values(0)
    return df

data = load_data(ticker)

# Create Plotly Candlestick Chart
fig = go.Figure(data=[go.Candlestick(
    x=data.index,
    open=data['Open'],
    high=data['High'],
    low=data['Low'],
    close=data['Close'],
    name=ticker
)])

# Configure layout with Logarithmic scale enabled by default
fig.update_layout(
    title=f"Live Chart: {selected_name}",
    yaxis_type="log",  # <--- Forces Logarithmic scale by default
    template="plotly_dark",
    height=chart_height,
    xaxis_rangeslider_visible=False
)

# Render the chart in Streamlit
st.plotly_chart(fig, use_container_width=True)