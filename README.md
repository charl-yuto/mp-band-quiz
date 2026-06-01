# MP Band Quiz v8 Search + Electronic System

This is a v8-style MP Band Quiz app with:

- Interactive Plotly band structure / DOS / crystal structure / BZ-kpath UI
- Random quiz mode
- Search quiz mode: `mp-149`, `Si`, `Fe2O3`, `Fe-O`, etc.
- Electron-system filter:
  - any
  - s/p system
  - d-electron system
  - f-electron system
  - exclude f-block
- Orbital selector for DOS display/filtering: `s`, `p`, `d`, `f`
- f-block diagnostic:
  - detects when a rare-earth/actinide element is present
  - detects whether MP projected DOS actually contains f-DOS
  - can exclude materials where f-DOS is missing, which often means f electrons were treated as core-like or were not included in the stored projection
- GitHub / Render ready: Dockerfile and render.yaml included

## Local one-URL mode

```bash
cd mp_band_quiz_real_v8_search_electronic
export MP_API_KEY='your_materials_project_api_key'
PORT=8010 ./run_one_url.sh
```

Open:

```text
http://127.0.0.1:8010/
```

## Render deployment

Push this directory to GitHub, then create a Render Web Service from the repository.
Use the included Dockerfile. Set this environment variable in Render:

```text
MP_API_KEY = your_materials_project_api_key
```

For Docker deployment, build/start commands can be empty because the Dockerfile defines them.

## Recommended quiz settings

For fair material-identification quizzes:

```text
電子系: なんでも / d電子系のみ / s/p系のみ
希土類・アクチノイド除外: ON for beginner mode
f-DOS欠損を除外: ON
表示・判定に使う軌道: s,p,d,f all ON unless you intentionally hide some orbitals
```

For f-electron quizzes:

```text
電子系: f電子系のみ
希土類・アクチノイド除外: OFF
f-DOS欠損を除外: ON
軌道: f ON
```
