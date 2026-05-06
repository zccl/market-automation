# A 股短线预测自动化

这个项目用 GitHub Actions 在云端定时运行，不需要本地电脑一直开机。

## 运行时间

- 北京时间每天 09:00：抓取昨晚美股和今早日韩股市资讯，生成 `history/YYYYMMDD/predictions.json`
- 北京时间每天 18:30：抓取 A 股真实收盘结果，生成 `history/YYYYMMDD/results.json`，并更新 `history/YYYYMMDD/scores.csv`

GitHub Actions 的 cron 使用 UTC，所以配置里分别是 `01:00 UTC` 和 `10:30 UTC`。

## 需要配置的 GitHub Secret

在仓库页面进入 `Settings -> Secrets and variables -> Actions -> New repository secret`：

- `OPENAI_API_KEY`：必填，用于生成预测 JSON
- `OPENAI_MODEL`：可选，默认 `gpt-4o-mini`

## 本地测试

```powershell
pip install -r requirements.txt
$env:OPENAI_API_KEY="你的 key"
python scripts/market_bot.py predict
python scripts/market_bot.py result
python scripts/market_bot.py score --date 2026-05-06
```

## 输出结构

```text
history/
  20260507/
    predictions.json
    results.json
    scores.csv
```

`predictions.json` 会保持为你要求的严格 JSON 字段，方便后续回测和优化。
