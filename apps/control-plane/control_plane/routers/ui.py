from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Instance
from control_plane.models import User
from control_plane.services.provisioner import Provisioner

router = APIRouter(tags=["ui"])


def _page(title: str, body: str) -> str:
    return f"""
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>{title}</title>
    <style>
      body {{ font-family: ui-sans-serif, system-ui; margin: 2rem; color: #111; }}
      h1 {{ font-size: 1.5rem; }}
      .card {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 1rem; margin-bottom: 1rem; }}
      label {{ display: block; margin-top: 0.5rem; }}
      input {{ width: 320px; padding: 0.4rem; margin-top: 0.25rem; }}
      button {{ margin-top: 0.75rem; padding: 0.5rem 0.8rem; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 0.5rem; }}
      th, td {{ text-align: left; padding: 0.5rem; border-bottom: 1px solid #e5e7eb; }}
      small {{ color: #6b7280; }}
    </style>
  </head>
  <body>
    {body}
  </body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
def home():
    body = """
    <h1>Longhouse Control Plane</h1>
    <div class=\"card\">
      <p>Status: <strong>OK</strong></p>
      <p><a href=\"/admin\">Admin provisioning</a></p>
    </div>
    """
    return _page("Longhouse Control Plane", body)


@router.get("/admin", response_class=HTMLResponse)
def admin(db: Session = Depends(get_db)):
    rows = db.query(Instance, User).join(User, Instance.user_id == User.id).all()
    table_rows = "".join(
        f"<tr><td>{inst.id}</td><td>{user.email}</td><td>{inst.subdomain}</td><td>{inst.status}</td></tr>"
        for inst, user in rows
    )
    if not table_rows:
        table_rows = "<tr><td colspan=4><em>No instances yet</em></td></tr>"

    body = f"""
    <h1>Provision Instance</h1>
    <div class=\"card\">
      <form method=\"post\" action=\"/admin/provision\">
        <label>Admin token <input type=\"password\" name=\"token\" required></label>
        <label>Email <input type=\"email\" name=\"email\" required></label>
        <label>Subdomain <input type=\"text\" name=\"subdomain\" required></label>
        <button type=\"submit\">Provision</button>
      </form>
      <small>Will create user + provision instance container.</small>
    </div>
    <div class=\"card\">
      <h2>Instances</h2>
      <table>
        <thead><tr><th>ID</th><th>Email</th><th>Subdomain</th><th>Status</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
    """
    return _page("Admin Provisioning", body)


@router.post("/admin/provision", response_class=HTMLResponse)
def admin_provision(
    token: str = Form(...),
    email: str = Form(...),
    subdomain: str = Form(...),
    db: Session = Depends(get_db),
):
    if token != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")

    email = email.strip().lower()
    subdomain = subdomain.strip().lower()

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)

    existing = db.query(Instance).filter(Instance.user_id == user.id).first()
    if existing:
        body = f"<p>Instance already exists for {email} ({existing.subdomain}).</p><p><a href=\"/admin\">Back</a></p>"
        return _page("Provisioning", body)

    provisioner = Provisioner()
    result = provisioner.provision_instance(subdomain, owner_email=email)

    instance = Instance(
        user_id=user.id,
        subdomain=subdomain,
        container_name=result.container_name,
        data_path=result.data_path,
        status="provisioning",
    )
    db.add(instance)
    db.commit()

    body = (
        f"<p>Provisioned <strong>{subdomain}</strong> for {email}.</p>"
        f"<p>Container: {result.container_name}</p>"
        f"<p><a href=\"/admin\">Back</a></p>"
    )
    return _page("Provisioned", body)
