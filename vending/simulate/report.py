"""Placeholder: replaced below."""


def write(conn, st, out, opts):
    import json
    (out / "runs.json").write_text(json.dumps(st.runs, indent=1, default=str))
