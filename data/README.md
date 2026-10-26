# data/

The dataset is not committed. Put it here before running anything.

Expected file: `ma_transactions_500.csv`. It has 500 rows of M&A transactions plus a header row:

```
transaction_id,target_company,acquirer,sector,sub_sector,deal_year,deal_quarter,deal_type,geography,financing_type,deal_size_mm,target_revenue_mm,target_ebitda_mm,ebitda_margin_pct,revenue_growth_pct,ev_ebitda_multiple,ev_revenue_multiple,synergy_pct_of_deal,outcome,strategic_rationale_tags,num_bidders,days_to_close,acquirer_type,target_ownership_pre
```

`strategic_rationale_tags` is pipe-delimited (e.g. `Technology Capability|Geographic Expansion`).
