# MP Band Quiz v8

v8 は「問題キャッシュや事前取得でランダム性が落ちる」問題を避けるため、**デフォルトで完全な問題キャッシュを使いません**。

## 何を速くしたか

- `has_props=["bandstructure", "dos"]` を使える環境では、band/DOS を持つ候補だけに絞ります。
- 候補数の既定値を軽くしました。
- 失敗候補を減らすため、候補検索を先に軽く絞ります。
- バンド/DOS は Plotly/JSON で描画し、エネルギー範囲変更は frontend だけで反映します。
- 数値入力を `type="text"` にし、`-3` のような負の数を自然に入力できるようにしました。

## 推奨設定

画面の「検索高速化」では、まず以下を推奨します。

```text
band/DOSがある候補に絞る: ON
最近出た物質を避ける: ON
候補リストを保存: OFF
候補リストを毎回作り直す: ON
候補数: 80〜150
候補チャンク数: 1
試行数: 4〜8
```

この設定は、問題キャッシュや事前取得を使わずに、なるべくランダム性を保ったまま検索を軽くする設定です。

## 起動

```bash
cd ~/material/mp_band_quiz_real_v8
export MP_API_KEY='your_api_key'
./run_one_url.sh
```

ブラウザで:

```text
http://127.0.0.1:8000/
```

## 外部 URL 共有

```bash
cd ~/material/mp_band_quiz_real_v8
export MP_API_KEY='your_api_key'
./run_public_cloudflare.sh
```

表示された `https://...trycloudflare.com` を共有してください。

## cloudflared がない場合

```bash
./install_cloudflared_user.sh
source ~/.bashrc
cloudflared --version
```

## 既存キャッシュを消したい場合

```bash
cd ~/material/mp_band_quiz_real_v8
rm -rf backend/cache/quizzes backend/cache/index.json
rm -rf backend/cache/candidate_pools
rm -f backend/cache/recent_history.json
```

## 設定の意味

### 出題条件

- 種類: 単体/化合物/なんでも
- 金属性: 金属/非金属/なんでも
- 安定性: 安定のみ/問わない
- 元素数 min/max
- k-path convention

### 検索高速化

- band/DOSがある候補に絞る: `has_props` を使って、失敗候補を減らします。通常 ON。
- 最近出た物質を避ける: 直近の mp-id を避けます。
- 候補リストを保存: mp-id 一覧だけ保存します。ランダム性重視なら OFF。
- 候補リストを毎回作り直す: 保存済み候補リストを使わずに再検索します。
- 候補数: summary 検索で取る候補数。大きいほど候補は増えますが遅くなります。
- 候補チャンク数: 候補数をさらに増やします。まずは 1 推奨。
- 試行数: band/DOS が取れる候補を試す最大回数。

