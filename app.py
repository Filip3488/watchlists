import streamlit as st
import streamlit.components.v1 as components

# Set page layout to wide
st.set_page_config(page_title="TradingView Watchlist", layout="wide")

st.title("📈 Interactive Stock Watchlist with TradingView")

# Define your watchlist with TradingView exchange prefixes
stocks = {
    "Apple (AAPL)": "NASDAQ:AAPL",
    "Tesla (TSLA)": "NASDAQ:TSLA",
    "Microsoft (MSFT)": "NASDAQ:MSFT",
    "NVIDIA (NVDA)": "NASDAQ:NVDA",
    "S&P 500 ETF (SPY)": "AMEX:SPY",
    "Bitcoin (BTCUSD)": "BINANCE:BTCUSDT"
}

# Sidebar selection
st.sidebar.header("Navigation")
selected_name = st.sidebar.radio("Select a Stock:", list(stocks.keys()))
selected_ticker = stocks[selected_name]

# Sidebar user controls for chart customization
st.sidebar.divider()
st.sidebar.header("⚙️ Chart Settings")
chart_height = st.sidebar.slider("Chart Height (pixels)", min_value=400, max_value=1500, value=1000, step=50)
theme_choice = st.sidebar.selectbox("Theme", ["dark", "light"], index=0)

# Option for Logarithmic Scale (Default set to True)
log_scale_choice = st.sidebar.toggle("Logarithmic Scale", value=True)
scale_mode = "1" if not log_scale_choice else "2" # TradingView internal scale mode handling where applicable

# Main content header layout (Title + External Link Button)
col1, col2 = st.columns([0.75, 0.25])
with col1:
    st.subheader(f"Live Chart: {selected_name}")

with col2:
    # Correct URL format using the exchange:ticker directly (preserving the colon)
    tv_web_link = f"https://www.tradingview.com/chart/?symbol={selected_ticker}"
    st.markdown("<br>", unsafe_allow_html=True) # Vertical alignment spacing
    st.link_button("🔗 Open on TradingView", tv_web_link, use_container_width=True)

# TradingView Advanced Real-Time Chart Widget HTML with full height scaling
tradingview_html = f"""
<!DOCTYPE html>
<html>
<head>
<style>
  body, html {{
    margin: 0;
    padding: 0;
    width: 100%;
    height: 100%;
    background-color: {'#131722' if theme_choice == 'dark' else '#ffffff'};
  }}
</style>
</head>
<body>
<!-- TradingView Widget BEGIN -->
<div class="tradingview-widget-container" style="height:100%;width:100%">
  <div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
  {{
    "autosize": true,
    "symbol": "{selected_ticker}",
    "interval": "D",
    "timezone": "Etc/UTC",
    "theme": "{theme_choice}",
    "style": "1",
    "locale": "en",
    "allow_symbol_change": true,
    "calendar": false,
    "support_host": "https://www.tradingview.com"
  }}
  </script>
</div>
<!-- TradingView Widget END -->
</body>
</html>
"""

# Render the HTML component inside Streamlit using the user-controlled height slider
components.html(tradingview_html, height=chart_height, scrolling=False)