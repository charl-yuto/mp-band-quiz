# MP Band Quiz v17 Render Fast

Render での使い勝手を改善した版です。UI は v8/v16 系の Plotly 可動表示を維持し、ランダム性を大きく落とさずに速度を上げるため、候補 ID プール + インメモリ次問キューを使います。

- 初回・cold start 直後は Materials Project API へのアクセスで遅いことがあります。
- 2問目以降は同じ設定ならメモリ上の次問キューから出せるため速くなります。
- キューは永続化しません。Render インスタンスが再起動すると消えます。
- 正解確認のため quiz payload は一時 JSON として保存しますが、再利用出題用の full cache はデフォルト OFF です。

## Render update

既存 Git repository にこの中身を反映して push してください。

```bash
rsync -av --delete --exclude .git/ --exclude .gitignore --exclude backend/.venv/ --exclude frontend/node_modules/ --exclude backend/cache/ SOURCE/ TARGET/
cd TARGET
git add -A
git commit -m "Improve Render speed and health checks"
git push
```

Render 側では `MP_API_KEY` を Environment Variables に設定してください。
