# Drop sponsor / ISO contracts here

Put the signed **and** unsigned sponsor agreements (with their **Schedule A** fee
schedules) in this folder — PDF, DOCX, or images are all fine. One file per
sponsor is ideal; multi-sponsor PDFs are fine too.

**Confidentiality:** everything in this folder except this README is git-ignored
(see `.gitignore`) — the contracts stay on this machine and are **never committed
or pushed to GitHub.**

Once the files are here, Claude will read each Schedule A line by line to extract,
per sponsor:
- NexusPay buy cost and the merchant's cost (powers the "cheapest-to-merchant +
  highest-payout" ranking — replaces the hardcoded guesses in `quotes.py`)
- real `brand_risk` / `strategic` scores (replaces the provisional reputation
  guesses in `app/services/provider_selection.py`)
- cash-discount / dual-pricing terms and any portfolio-ownership / exit clauses
