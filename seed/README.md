# seed

Hand-collected prices for the layer 2 index, collected by a person (not the bot):
`seed/<yyyy-mm>/<region>.csv`, one file per market, header:

```
partId,sellerGroup,price,currency,observed,availability
```

Each row also needs its source link and date in the private evidence. Seed prices only ever feed an index (marked
`seeded` in the price file); they never make a market band.
