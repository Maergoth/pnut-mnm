# PNUT M&M 1.0.30

- Leaderboard cards, local coin summaries and copied stat blocks display captured
  coin totals using the fewest platinum, gold, silver and copper coins.
- Each denomination converts at 100 to 1, and zero parts are omitted: 1,050 copper
  displays as **10s 50c**; 1,000,000 copper displays as **1p**.
- Coin sorting continues to compare the complete value, so **9s** sorts below
  **10s 50c**. Existing captured totals retain their full value.
- Ranks keeps the last complete identity and stat display while the screen reader
  is busy. Values update when they change, with expired readings and capture
  eligibility still reflected and Copy/Submit validated when clicked.
