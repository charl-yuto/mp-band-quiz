# MP Band Quiz v15

Materials Project の band structure / orbital DOS / 結晶構造 / BZ-kpath を使う物質当てクイズです。

## ローカル起動

```bash
export MP_API_KEY='your_api_key'
PORT=8010 ./run_one_url.sh
```

ブラウザ: `http://127.0.0.1:8010/`

## 一時URL公開

```bash
export MP_API_KEY='your_api_key'
PORT=8010 ./run_public_cloudflare.sh
```

表示された `https://...trycloudflare.com` を共有します。この方式はターミナルを閉じると止まります。

## 常時公開

Render/Fly.io/VPS/Docker を使います。詳細は `README_DEPLOY.md` を見てください。

## 重要な設定

- ランダム方式: `バランス乱数（推奨）`
- 電子系: `なんでも`, `s/p系`, `d電子系`, `f電子系`, `fブロック除外`
- 軌道: `s,p,d,f` を個別に選択可能
- エネルギー範囲: 初期値 `-12 eV` から `+12 eV`

