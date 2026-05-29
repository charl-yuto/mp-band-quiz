# MP Band Quiz v16 Speed

v8 系の Plotly 可動 UI を維持しつつ、URL版での遅いランダム出題と intermittent error を減らすための更新版です。

## 主な変更

- バンド/DOS/結晶構造/BZ-kpath の可動 UI は維持。
- 正解時の簡易 confetti effect を維持。
- デフォルト表示範囲を `-12 eV` から `+12 eV` に拡大。
- 組成表示を「構成比」として匿名組成式だけ表示。実組成式は正解表示まで隠します。
- ランダム方式のデフォルトを `fast_pool` に変更。
  - 完全な問題キャッシュではなく、候補 mp-id だけをメモリ保持します。
  - 同じ問題の再利用ではないため、キャッシュ偏りをかなり抑えつつ、2問目以降の候補検索を高速化します。
- MP API / backend 例外は JSON で返すようにし、frontend の `Unexpected token I` 系エラーを回避。
- 候補探索を複数ラウンド化し、band/DOS が取れない候補は自動 skip。
- 検索出題、電子系選択、軌道選択を維持。

## ローカル起動

```bash
export MP_API_KEY='your_api_key'
PORT=8010 ./run_one_url.sh
```

ブラウザ: `http://127.0.0.1:8010/`

## Render 更新

既存 repository をこの中身で置き換えて push してください。

```bash
git add .
git commit -m "Speed up random quiz and improve robust live mode"
git push
```

Render の Auto Deploy が ON なら自動更新されます。

## 推奨設定

- ランダム方式: `高速候補プール（推奨）`
- band/DOS がある候補に絞る: ON
- 最近出た物質を避ける: ON
- 候補IDプール数: 300〜800
- 候補探索回数: 1〜3
- 試行数: 12〜24
- 電子系: なんでも / d電子系 / s-p系 / f電子系
- f-DOS欠損除外: ON

## 注意

Render Free では service が sleep した直後の初回アクセスが遅くなります。これは Render 側の cold start です。アプリ内の高速化は、service 起動後の出題速度に効きます。
