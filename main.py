from datetime import date, timedelta, datetime
from typing import Optional, List
from fastapi import FastAPI, Depends, Request, Form, HTTPException, status as http_status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
import json

from database import engine, get_db, Base
import models
import schemas

Base.metadata.create_all(bind=engine)

app = FastAPI(title="漆树采收管理系统")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def recalculate_incision_stats(db: Session, incision_id: int):
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        return
    harvests = db.query(models.HarvestBatch).filter(models.HarvestBatch.incision_id == incision_id).all()
    incision.total_harvests = len(harvests)
    incision.total_yield = sum(h.yield_amount for h in harvests)
    incision.avg_yield = incision.total_yield / incision.total_harvests if incision.total_harvests > 0 else 0.0
    db.commit()


def check_recovery_period(db: Session, incision_id: int, harvest_date: date) -> tuple:
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        return True, ""
    last_harvest = db.query(models.HarvestBatch).filter(
        models.HarvestBatch.incision_id == incision_id
    ).order_by(models.HarvestBatch.harvest_date.desc()).first()
    if last_harvest:
        recovery_end = last_harvest.harvest_date + timedelta(days=incision.recovery_days)
        if harvest_date < recovery_end:
            return False, f"该割口尚在恢复期，下次可采收日期为 {recovery_end.strftime('%Y-%m-%d')}"
    return True, ""


def check_recovery_period_for_plan(db: Session, incision_id: int, plan_date: date) -> tuple:
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        return True, ""
    last_harvest = db.query(models.HarvestBatch).filter(
        models.HarvestBatch.incision_id == incision_id
    ).order_by(models.HarvestBatch.harvest_date.desc()).first()
    if last_harvest:
        recovery_end = last_harvest.harvest_date + timedelta(days=incision.recovery_days)
        if plan_date < recovery_end:
            return False, f"计划日期 {plan_date.strftime('%Y-%m-%d')} 尚在恢复期内，下次可采收日期为 {recovery_end.strftime('%Y-%m-%d')}"
    return True, ""


def check_abnormal_status(db: Session, incision_id: int) -> tuple:
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        return True, ""
    last_incision_obs = db.query(models.RecoveryObservation).filter(
        models.RecoveryObservation.incision_id == incision_id
    ).order_by(models.RecoveryObservation.observation_date.desc()).first()
    if last_incision_obs and last_incision_obs.is_abnormal:
        return False, f"该割口最近一次恢复观察（{last_incision_obs.observation_date.strftime('%Y-%m-%d')}）为异常状态，需先处理异常后才能计划采收"
    last_tree_obs = db.query(models.RecoveryObservation).filter(
        models.RecoveryObservation.tree_id == incision.tree_id,
        models.RecoveryObservation.incision_id == None
    ).order_by(models.RecoveryObservation.observation_date.desc()).first()
    if last_tree_obs and last_tree_obs.is_abnormal:
        return False, f"该漆树最近一次整树恢复观察（{last_tree_obs.observation_date.strftime('%Y-%m-%d')}）为异常状态，需先处理异常后才能计划采收"
    return True, ""


def get_upcoming_harvest_plans(db: Session, days: int = 30) -> List[dict]:
    today = date.today()
    end_date = today + timedelta(days=days)
    plans = db.query(models.HarvestPlan).filter(
        and_(
            models.HarvestPlan.status == "待执行",
            models.HarvestPlan.plan_date >= today,
            models.HarvestPlan.plan_date <= end_date
        )
    ).order_by(models.HarvestPlan.plan_date).all()
    results = []
    for plan in plans:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == plan.tree_id).first()
        incision = db.query(models.Incision).filter(models.Incision.id == plan.incision_id).first()
        results.append({
            "id": plan.id,
            "tree_code": tree.tree_code if tree else "未知",
            "incision_code": incision.incision_code if incision else "未知",
            "plan_date": plan.plan_date,
            "harvest_method": plan.harvest_method,
            "person_in_charge": plan.person_in_charge,
            "days_left": (plan.plan_date - today).days,
            "status": plan.status
        })
    return results


def get_abnormal_warnings(db: Session) -> List[dict]:
    observations = db.query(models.RecoveryObservation).filter(
        models.RecoveryObservation.is_abnormal == True
    ).order_by(models.RecoveryObservation.observation_date.desc()).all()
    results = []
    for obs in observations:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == obs.tree_id).first()
        incision = db.query(models.Incision).filter(models.Incision.id == obs.incision_id).first() if obs.incision_id else None
        results.append({
            "id": obs.id,
            "tree_code": tree.tree_code if tree else "未知",
            "incision_code": incision.incision_code if incision else "整树观察",
            "observation_date": obs.observation_date,
            "tree_condition": obs.tree_condition,
            "treatment_suggestion": obs.treatment_suggestion,
            "observer": obs.observer
        })
    return results


def update_harvest_plan_status(db: Session):
    today = date.today()
    plans = db.query(models.HarvestPlan).filter(
        models.HarvestPlan.status == "待执行"
    ).all()
    for plan in plans:
        if plan.plan_date < today:
            plan.status = "已过期"
    db.commit()


def get_cost_warnings(db: Session) -> dict:
    high_cost_warnings = []
    low_output_warnings = []
    trees = db.query(models.LacquerTree).all()
    for tree in trees:
        tree_records = db.query(models.MaintenanceRecord).filter(
            models.MaintenanceRecord.tree_id == tree.id
        ).all()
        tree_cost = sum(r.total_cost for r in tree_records)
        tree_labor = sum(r.labor_hours for r in tree_records)
        incisions = db.query(models.Incision).filter(models.Incision.tree_id == tree.id).all()
        tree_yield = sum(inc.total_yield for inc in incisions)
        if tree_cost > 0 and tree_yield > 0:
            unit_cost = tree_cost / tree_yield
            if unit_cost > 500:
                high_cost_warnings.append({
                    "tree_code": tree.tree_code,
                    "tree_id": tree.id,
                    "total_cost": round(tree_cost, 2),
                    "total_yield": round(tree_yield, 2),
                    "unit_cost": round(unit_cost, 2),
                    "type": "tree"
                })
        elif tree_cost > 0 and tree_yield == 0:
            low_output_warnings.append({
                "tree_code": tree.tree_code,
                "tree_id": tree.id,
                "total_cost": round(tree_cost, 2),
                "total_labor": round(tree_labor, 2),
                "type": "tree"
            })
    incisions = db.query(models.Incision).all()
    for inc in incisions:
        inc_records = db.query(models.MaintenanceRecord).filter(
            models.MaintenanceRecord.incision_id == inc.id
        ).all()
        inc_cost = sum(r.total_cost for r in inc_records)
        inc_labor = sum(r.labor_hours for r in inc_records)
        if inc_cost > 0 and inc.total_yield > 0:
            unit_cost = inc_cost / inc.total_yield
            if unit_cost > 500:
                tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == inc.tree_id).first()
                high_cost_warnings.append({
                    "incision_code": inc.incision_code,
                    "tree_code": tree.tree_code if tree else "未知",
                    "incision_id": inc.id,
                    "total_cost": round(inc_cost, 2),
                    "total_yield": round(inc.total_yield, 2),
                    "unit_cost": round(unit_cost, 2),
                    "type": "incision"
                })
        elif inc_cost > 0 and inc.total_yield == 0:
            tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == inc.tree_id).first()
            low_output_warnings.append({
                "incision_code": inc.incision_code,
                "tree_code": tree.tree_code if tree else "未知",
                "incision_id": inc.id,
                "total_cost": round(inc_cost, 2),
                "total_labor": round(inc_labor, 2),
                "type": "incision"
            })
    high_cost_warnings.sort(key=lambda x: x["unit_cost"] if "unit_cost" in x else 0, reverse=True)
    low_output_warnings.sort(key=lambda x: x["total_cost"], reverse=True)
    return {"high_cost": high_cost_warnings[:10], "low_output": low_output_warnings[:10]}


def recalculate_all_reminders(db: Session):
    update_harvest_plan_status(db)


def get_next_harvest_dates(db: Session) -> List[dict]:
    results = []
    incisions = db.query(models.Incision).filter(models.Incision.status == "活跃").all()
    today = date.today()
    for inc in incisions:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == inc.tree_id).first()
        last_harvest = db.query(models.HarvestBatch).filter(
            models.HarvestBatch.incision_id == inc.id
        ).order_by(models.HarvestBatch.harvest_date.desc()).first()
        if last_harvest:
            next_date = last_harvest.harvest_date + timedelta(days=inc.recovery_days)
        else:
            next_date = inc.incision_date + timedelta(days=inc.recovery_days)
        status = "待采收" if next_date <= today else "恢复中"
        results.append({
            "tree_code": tree.tree_code if tree else "未知",
            "incision_code": inc.incision_code,
            "method": inc.method,
            "next_harvest": next_date,
            "days_left": (next_date - today).days,
            "status": status
        })
    results.sort(key=lambda x: x["days_left"])
    return results


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    recalculate_all_reminders(db)
    tree_count = db.query(func.count(models.LacquerTree.id)).scalar() or 0
    incision_count = db.query(func.count(models.Incision.id)).scalar() or 0
    harvest_count = db.query(func.count(models.HarvestBatch.id)).scalar() or 0
    total_yield = db.query(func.sum(models.HarvestBatch.yield_amount)).scalar() or 0.0
    abnormal_count = db.query(func.count(models.RecoveryObservation.id)).filter(
        models.RecoveryObservation.is_abnormal == True
    ).scalar() or 0
    plan_count = db.query(func.count(models.HarvestPlan.id)).filter(
        models.HarvestPlan.status == "待执行"
    ).scalar() or 0
    harvest_reminders = get_next_harvest_dates(db)
    ready_count = sum(1 for r in harvest_reminders if r["status"] == "待采收")
    upcoming_plans = get_upcoming_harvest_plans(db, days=30)
    abnormal_warnings = get_abnormal_warnings(db)
    cost_warnings = get_cost_warnings(db)
    total_maintenance_cost = db.query(func.sum(models.MaintenanceRecord.total_cost)).scalar() or 0.0
    total_labor_hours = db.query(func.sum(models.MaintenanceRecord.labor_hours)).scalar() or 0.0
    return templates.TemplateResponse("index.html", {
        "request": request,
        "tree_count": tree_count,
        "incision_count": incision_count,
        "harvest_count": harvest_count,
        "total_yield": round(total_yield, 2),
        "abnormal_count": abnormal_count,
        "plan_count": plan_count,
        "harvest_reminders": harvest_reminders[:10],
        "ready_count": ready_count,
        "upcoming_plans": upcoming_plans[:10],
        "abnormal_warnings": abnormal_warnings[:10],
        "cost_warnings": cost_warnings,
        "total_maintenance_cost": round(total_maintenance_cost, 2),
        "total_labor_hours": round(total_labor_hours, 1)
    })


@app.get("/trees", response_class=HTMLResponse)
async def list_trees(request: Request, db: Session = Depends(get_db)):
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    return templates.TemplateResponse("trees/list.html", {"request": request, "trees": trees})


@app.get("/trees/new", response_class=HTMLResponse)
async def new_tree_form(request: Request):
    return templates.TemplateResponse("trees/form.html", {"request": request, "tree": None, "errors": {}})


@app.post("/trees/new")
async def create_tree(
    request: Request,
    db: Session = Depends(get_db),
    tree_code: str = Form(...),
    species: Optional[str] = Form(None),
    age: Optional[int] = Form(None),
    location: Optional[str] = Form(None),
    altitude: Optional[float] = Form(None),
    soil_type: Optional[str] = Form(None),
    planting_date: Optional[str] = Form(None),
    status: str = Form("正常"),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    existing = db.query(models.LacquerTree).filter(models.LacquerTree.tree_code == tree_code).first()
    if existing:
        errors["tree_code"] = "漆树编号已存在，不能重复"
    if errors:
        return templates.TemplateResponse("trees/form.html", {
            "request": request,
            "tree": None,
            "form_data": {
                "tree_code": tree_code, "species": species, "age": age,
                "location": location, "altitude": altitude, "soil_type": soil_type,
                "planting_date": planting_date, "status": status, "remarks": remarks
            },
            "errors": errors
        })
    tree = models.LacquerTree(
        tree_code=tree_code, species=species, age=age, location=location,
        altitude=altitude, soil_type=soil_type, status=status, remarks=remarks
    )
    if planting_date:
        tree.planting_date = datetime.strptime(planting_date, "%Y-%m-%d").date()
    db.add(tree)
    db.commit()
    return RedirectResponse("/trees", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/trees/{tree_id}", response_class=HTMLResponse)
async def view_tree(request: Request, tree_id: int, db: Session = Depends(get_db)):
    tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == tree_id).first()
    if not tree:
        raise HTTPException(status_code=404, detail="漆树不存在")
    incisions = db.query(models.Incision).filter(models.Incision.tree_id == tree_id).all()
    observations = db.query(models.RecoveryObservation).filter(
        models.RecoveryObservation.tree_id == tree_id
    ).order_by(models.RecoveryObservation.observation_date.desc()).all()
    return templates.TemplateResponse("trees/detail.html", {
        "request": request, "tree": tree, "incisions": incisions, "observations": observations
    })


@app.get("/trees/{tree_id}/edit", response_class=HTMLResponse)
async def edit_tree_form(request: Request, tree_id: int, db: Session = Depends(get_db)):
    tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == tree_id).first()
    if not tree:
        raise HTTPException(status_code=404, detail="漆树不存在")
    return templates.TemplateResponse("trees/form.html", {
        "request": request, "tree": tree, "errors": {}
    })


@app.post("/trees/{tree_id}/edit")
async def update_tree(
    request: Request,
    tree_id: int,
    db: Session = Depends(get_db),
    tree_code: str = Form(...),
    species: Optional[str] = Form(None),
    age: Optional[int] = Form(None),
    location: Optional[str] = Form(None),
    altitude: Optional[float] = Form(None),
    soil_type: Optional[str] = Form(None),
    planting_date: Optional[str] = Form(None),
    status: str = Form("正常"),
    remarks: Optional[str] = Form(None)
):
    tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == tree_id).first()
    if not tree:
        raise HTTPException(status_code=404, detail="漆树不存在")
    errors = {}
    existing = db.query(models.LacquerTree).filter(
        models.LacquerTree.tree_code == tree_code, models.LacquerTree.id != tree_id
    ).first()
    if existing:
        errors["tree_code"] = "漆树编号已存在，不能重复"
    if errors:
        return templates.TemplateResponse("trees/form.html", {
            "request": request, "tree": tree, "errors": errors
        })
    tree.tree_code = tree_code
    tree.species = species
    tree.age = age
    tree.location = location
    tree.altitude = altitude
    tree.soil_type = soil_type
    tree.status = status
    tree.remarks = remarks
    if planting_date:
        tree.planting_date = datetime.strptime(planting_date, "%Y-%m-%d").date()
    db.commit()
    return RedirectResponse(f"/trees/{tree_id}", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/trees/{tree_id}/delete")
async def delete_tree(tree_id: int, db: Session = Depends(get_db)):
    tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == tree_id).first()
    if not tree:
        raise HTTPException(status_code=404, detail="漆树不存在")
    db.delete(tree)
    db.commit()
    return RedirectResponse("/trees", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/incisions", response_class=HTMLResponse)
async def list_incisions(request: Request, db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).order_by(models.Incision.id.desc()).all()
    return templates.TemplateResponse("incisions/list.html", {"request": request, "incisions": incisions})


@app.get("/incisions/new", response_class=HTMLResponse)
async def new_incision_form(request: Request, db: Session = Depends(get_db)):
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
    return templates.TemplateResponse("incisions/form.html", {
        "request": request, "incision": None, "trees": trees, "methods": methods, "errors": {}
    })


@app.post("/incisions/new")
async def create_incision(
    request: Request,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    incision_code: str = Form(...),
    position: str = Form(...),
    height: Optional[float] = Form(None),
    method: str = Form(...),
    incision_date: str = Form(...),
    recovery_days: int = Form(7),
    status: str = Form("活跃"),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    try:
        inc_date = datetime.strptime(incision_date, "%Y-%m-%d").date()
        if inc_date > date.today():
            errors["incision_date"] = "割口日期不能晚于当前日期"
    except ValueError:
        errors["incision_date"] = "日期格式不正确"
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
        return templates.TemplateResponse("incisions/form.html", {
            "request": request, "incision": None, "trees": trees, "methods": methods,
            "errors": errors, "form_data": {
                "tree_id": tree_id, "incision_code": incision_code, "position": position,
                "height": height, "method": method, "incision_date": incision_date,
                "recovery_days": recovery_days, "status": status, "remarks": remarks
            }
        })
    incision = models.Incision(
        tree_id=tree_id, incision_code=incision_code, position=position,
        height=height, method=method, incision_date=inc_date,
        recovery_days=recovery_days, status=status, remarks=remarks
    )
    db.add(incision)
    db.commit()
    return RedirectResponse("/incisions", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/incisions/{incision_id}/edit", response_class=HTMLResponse)
async def edit_incision_form(request: Request, incision_id: int, db: Session = Depends(get_db)):
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        raise HTTPException(status_code=404, detail="割口记录不存在")
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
    return templates.TemplateResponse("incisions/form.html", {
        "request": request, "incision": incision, "trees": trees, "methods": methods, "errors": {}
    })


@app.post("/incisions/{incision_id}/edit")
async def update_incision(
    request: Request,
    incision_id: int,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    incision_code: str = Form(...),
    position: str = Form(...),
    height: Optional[float] = Form(None),
    method: str = Form(...),
    incision_date: str = Form(...),
    recovery_days: int = Form(7),
    status: str = Form("活跃"),
    remarks: Optional[str] = Form(None)
):
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        raise HTTPException(status_code=404, detail="割口记录不存在")
    errors = {}
    try:
        inc_date = datetime.strptime(incision_date, "%Y-%m-%d").date()
        if inc_date > date.today():
            errors["incision_date"] = "割口日期不能晚于当前日期"
    except ValueError:
        errors["incision_date"] = "日期格式不正确"
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
        return templates.TemplateResponse("incisions/form.html", {
            "request": request, "incision": incision, "trees": trees, "methods": methods, "errors": errors
        })
    incision.tree_id = tree_id
    incision.incision_code = incision_code
    incision.position = position
    incision.height = height
    incision.method = method
    incision.incision_date = inc_date
    incision.recovery_days = recovery_days
    incision.status = status
    incision.remarks = remarks
    db.commit()
    recalculate_incision_stats(db, incision_id)
    recalculate_all_reminders(db)
    return RedirectResponse("/incisions", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/incisions/{incision_id}/delete")
async def delete_incision(incision_id: int, db: Session = Depends(get_db)):
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        raise HTTPException(status_code=404, detail="割口记录不存在")
    db.delete(incision)
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/incisions", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/harvests", response_class=HTMLResponse)
async def list_harvests(request: Request, db: Session = Depends(get_db)):
    harvests = db.query(models.HarvestBatch).order_by(models.HarvestBatch.harvest_date.desc()).all()
    return templates.TemplateResponse("harvests/list.html", {"request": request, "harvests": harvests})


@app.get("/harvests/new", response_class=HTMLResponse)
async def new_harvest_form(request: Request, plan_id: Optional[int] = None, db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).filter(models.Incision.status == "活跃").all()
    weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
    grades = ["特级", "一级", "二级", "三级"]
    today = date.today().strftime("%Y-%m-%d")
    plan = None
    form_data = None
    if plan_id:
        plan = db.query(models.HarvestPlan).filter(models.HarvestPlan.id == plan_id).first()
        if plan:
            form_data = {
                "incision_id": plan.incision_id,
                "harvest_date": plan.plan_date.strftime("%Y-%m-%d"),
                "operator": plan.person_in_charge or "",
                "remarks": plan.remarks or ""
            }
    return templates.TemplateResponse("harvests/form.html", {
        "request": request, "harvest": None, "incisions": incisions,
        "weathers": weathers, "grades": grades, "errors": {}, "today": today,
        "form_data": form_data, "plan_id": plan_id
    })


@app.post("/harvests/new")
async def create_harvest(
    request: Request,
    db: Session = Depends(get_db),
    incision_id: int = Form(...),
    harvest_date: str = Form(...),
    yield_amount: float = Form(...),
    quality_grade: Optional[str] = Form(None),
    weather_id: Optional[int] = Form(None),
    operator: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None),
    plan_id: Optional[int] = Form(None)
):
    errors = {}
    try:
        h_date = datetime.strptime(harvest_date, "%Y-%m-%d").date()
        if h_date > date.today():
            errors["harvest_date"] = "采收日期不能晚于当前日期"
    except ValueError:
        errors["harvest_date"] = "日期格式不正确"
    if yield_amount < 0:
        errors["yield_amount"] = "出漆量不能为负数"
    if "harvest_date" not in errors:
        ok, msg = check_recovery_period(db, incision_id, h_date)
        if not ok:
            errors["recovery"] = msg
    if errors:
        incisions = db.query(models.Incision).filter(models.Incision.status == "活跃").all()
        weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
        grades = ["特级", "一级", "二级", "三级"]
        return templates.TemplateResponse("harvests/form.html", {
            "request": request, "harvest": None, "incisions": incisions,
            "weathers": weathers, "grades": grades, "errors": errors,
            "today": date.today().strftime("%Y-%m-%d"),
            "form_data": {
                "incision_id": incision_id, "harvest_date": harvest_date,
                "yield_amount": yield_amount, "quality_grade": quality_grade,
                "weather_id": weather_id, "operator": operator, "remarks": remarks
            },
            "plan_id": plan_id
        })
    harvest = models.HarvestBatch(
        incision_id=incision_id, harvest_date=h_date, yield_amount=yield_amount,
        quality_grade=quality_grade, weather_id=weather_id,
        operator=operator, remarks=remarks
    )
    db.add(harvest)
    db.flush()
    if plan_id:
        plan = db.query(models.HarvestPlan).filter(models.HarvestPlan.id == plan_id).first()
        if plan:
            plan.actual_harvest_id = harvest.id
            plan.status = "已执行"
    db.commit()
    recalculate_incision_stats(db, incision_id)
    recalculate_all_reminders(db)
    return RedirectResponse("/harvests", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/harvests/{harvest_id}/edit", response_class=HTMLResponse)
async def edit_harvest_form(request: Request, harvest_id: int, db: Session = Depends(get_db)):
    harvest = db.query(models.HarvestBatch).filter(models.HarvestBatch.id == harvest_id).first()
    if not harvest:
        raise HTTPException(status_code=404, detail="采收批次不存在")
    incisions = db.query(models.Incision).all()
    weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
    grades = ["特级", "一级", "二级", "三级"]
    return templates.TemplateResponse("harvests/form.html", {
        "request": request, "harvest": harvest, "incisions": incisions,
        "weathers": weathers, "grades": grades, "errors": {},
        "today": date.today().strftime("%Y-%m-%d")
    })


@app.post("/harvests/{harvest_id}/edit")
async def update_harvest(
    request: Request,
    harvest_id: int,
    db: Session = Depends(get_db),
    incision_id: int = Form(...),
    harvest_date: str = Form(...),
    yield_amount: float = Form(...),
    quality_grade: Optional[str] = Form(None),
    weather_id: Optional[int] = Form(None),
    operator: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None)
):
    harvest = db.query(models.HarvestBatch).filter(models.HarvestBatch.id == harvest_id).first()
    if not harvest:
        raise HTTPException(status_code=404, detail="采收批次不存在")
    errors = {}
    try:
        h_date = datetime.strptime(harvest_date, "%Y-%m-%d").date()
        if h_date > date.today():
            errors["harvest_date"] = "采收日期不能晚于当前日期"
    except ValueError:
        errors["harvest_date"] = "日期格式不正确"
    if yield_amount < 0:
        errors["yield_amount"] = "出漆量不能为负数"
    old_incision_id = harvest.incision_id
    if "harvest_date" not in errors and not errors.get("recovery"):
        target_incision_id = incision_id if incision_id else old_incision_id
        incision_for_check = db.query(models.Incision).filter(models.Incision.id == target_incision_id).first()
        if incision_for_check:
            last_harvest = db.query(models.HarvestBatch).filter(
                models.HarvestBatch.incision_id == target_incision_id,
                models.HarvestBatch.id != harvest_id
            ).order_by(models.HarvestBatch.harvest_date.desc()).first()
            if last_harvest:
                recovery_end = last_harvest.harvest_date + timedelta(days=incision_for_check.recovery_days)
                if h_date < recovery_end:
                    errors["recovery"] = f"该割口尚在恢复期，下次可采收日期为 {recovery_end.strftime('%Y-%m-%d')}"
    if errors:
        incisions = db.query(models.Incision).all()
        weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
        grades = ["特级", "一级", "二级", "三级"]
        return templates.TemplateResponse("harvests/form.html", {
            "request": request, "harvest": harvest, "incisions": incisions,
            "weathers": weathers, "grades": grades, "errors": errors,
            "today": date.today().strftime("%Y-%m-%d")
        })
    harvest.incision_id = incision_id
    harvest.harvest_date = h_date
    harvest.yield_amount = yield_amount
    harvest.quality_grade = quality_grade
    harvest.weather_id = weather_id
    harvest.operator = operator
    harvest.remarks = remarks
    db.commit()
    recalculate_incision_stats(db, old_incision_id)
    if old_incision_id != incision_id:
        recalculate_incision_stats(db, incision_id)
    recalculate_all_reminders(db)
    return RedirectResponse("/harvests", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/harvests/{harvest_id}/delete")
async def delete_harvest(harvest_id: int, db: Session = Depends(get_db)):
    harvest = db.query(models.HarvestBatch).filter(models.HarvestBatch.id == harvest_id).first()
    if not harvest:
        raise HTTPException(status_code=404, detail="采收批次不存在")
    incision_id = harvest.incision_id
    db.delete(harvest)
    db.commit()
    recalculate_incision_stats(db, incision_id)
    recalculate_all_reminders(db)
    return RedirectResponse("/harvests", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/weather", response_class=HTMLResponse)
async def list_weather(request: Request, db: Session = Depends(get_db)):
    weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
    return templates.TemplateResponse("weather/list.html", {"request": request, "weathers": weathers})


@app.get("/weather/new", response_class=HTMLResponse)
async def new_weather_form(request: Request):
    today = date.today().strftime("%Y-%m-%d")
    weather_types = ["晴", "多云", "阴", "小雨", "中雨", "大雨", "雾", "其他"]
    return templates.TemplateResponse("weather/form.html", {
        "request": request, "weather": None, "errors": {},
        "today": today, "weather_types": weather_types
    })


@app.post("/weather/new")
async def create_weather(
    request: Request,
    db: Session = Depends(get_db),
    record_date: str = Form(...),
    temperature: Optional[float] = Form(None),
    humidity: Optional[float] = Form(None),
    weather_type: Optional[str] = Form(None),
    wind_speed: Optional[float] = Form(None),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    try:
        r_date = datetime.strptime(record_date, "%Y-%m-%d").date()
        if r_date > date.today():
            errors["record_date"] = "记录日期不能晚于当前日期"
    except ValueError:
        errors["record_date"] = "日期格式不正确"
    if errors:
        weather_types = ["晴", "多云", "阴", "小雨", "中雨", "大雨", "雾", "其他"]
        return templates.TemplateResponse("weather/form.html", {
            "request": request, "weather": None, "errors": errors,
            "today": date.today().strftime("%Y-%m-%d"), "weather_types": weather_types
        })
    weather = models.WeatherCondition(
        record_date=r_date, temperature=temperature, humidity=humidity,
        weather_type=weather_type, wind_speed=wind_speed, remarks=remarks
    )
    db.add(weather)
    db.commit()
    return RedirectResponse("/weather", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/weather/{weather_id}/edit", response_class=HTMLResponse)
async def edit_weather_form(request: Request, weather_id: int, db: Session = Depends(get_db)):
    weather = db.query(models.WeatherCondition).filter(models.WeatherCondition.id == weather_id).first()
    if not weather:
        raise HTTPException(status_code=404, detail="天气记录不存在")
    weather_types = ["晴", "多云", "阴", "小雨", "中雨", "大雨", "雾", "其他"]
    return templates.TemplateResponse("weather/form.html", {
        "request": request, "weather": weather, "errors": {},
        "today": date.today().strftime("%Y-%m-%d"), "weather_types": weather_types
    })


@app.post("/weather/{weather_id}/edit")
async def update_weather(
    request: Request,
    weather_id: int,
    db: Session = Depends(get_db),
    record_date: str = Form(...),
    temperature: Optional[float] = Form(None),
    humidity: Optional[float] = Form(None),
    weather_type: Optional[str] = Form(None),
    wind_speed: Optional[float] = Form(None),
    remarks: Optional[str] = Form(None)
):
    weather = db.query(models.WeatherCondition).filter(models.WeatherCondition.id == weather_id).first()
    if not weather:
        raise HTTPException(status_code=404, detail="天气记录不存在")
    errors = {}
    try:
        r_date = datetime.strptime(record_date, "%Y-%m-%d").date()
        if r_date > date.today():
            errors["record_date"] = "记录日期不能晚于当前日期"
    except ValueError:
        errors["record_date"] = "日期格式不正确"
    if errors:
        weather_types = ["晴", "多云", "阴", "小雨", "中雨", "大雨", "雾", "其他"]
        return templates.TemplateResponse("weather/form.html", {
            "request": request, "weather": weather, "errors": errors,
            "today": date.today().strftime("%Y-%m-%d"), "weather_types": weather_types,
            "form_data": {
                "record_date": record_date, "temperature": temperature,
                "humidity": humidity, "weather_type": weather_type,
                "wind_speed": wind_speed, "remarks": remarks
            }
        })
    weather.record_date = r_date
    weather.temperature = temperature
    weather.humidity = humidity
    weather.weather_type = weather_type
    weather.wind_speed = wind_speed
    weather.remarks = remarks
    db.commit()
    return RedirectResponse("/weather", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/weather/{weather_id}/delete")
async def delete_weather(weather_id: int, db: Session = Depends(get_db)):
    weather = db.query(models.WeatherCondition).filter(models.WeatherCondition.id == weather_id).first()
    if not weather:
        raise HTTPException(status_code=404, detail="天气记录不存在")
    db.delete(weather)
    db.commit()
    return RedirectResponse("/weather", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/observations", response_class=HTMLResponse)
async def list_observations(request: Request, db: Session = Depends(get_db)):
    observations = db.query(models.RecoveryObservation).order_by(
        models.RecoveryObservation.observation_date.desc()
    ).all()
    return templates.TemplateResponse("observations/list.html", {"request": request, "observations": observations})


@app.get("/observations/new", response_class=HTMLResponse)
async def new_observation_form(request: Request, db: Session = Depends(get_db)):
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).all()
    conditions = ["优秀", "良好", "一般", "较差", "异常"]
    today = date.today().strftime("%Y-%m-%d")
    return templates.TemplateResponse("observations/form.html", {
        "request": request, "observation": None, "trees": trees,
        "incisions": incisions, "conditions": conditions,
        "errors": {}, "today": today
    })


@app.post("/observations/new")
async def create_observation(
    request: Request,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    observation_date: str = Form(...),
    incision_id: Optional[int] = Form(None),
    tree_condition: str = Form(...),
    bark_healing: Optional[str] = Form(None),
    sap_flow: Optional[str] = Form(None),
    leaf_condition: Optional[str] = Form(None),
    is_abnormal: Optional[str] = Form(None),
    treatment_suggestion: Optional[str] = Form(None),
    observer: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    try:
        o_date = datetime.strptime(observation_date, "%Y-%m-%d").date()
        if o_date > date.today():
            errors["observation_date"] = "观察日期不能晚于当前日期"
    except ValueError:
        errors["observation_date"] = "日期格式不正确"
    abnormal_flag = is_abnormal == "on"
    if abnormal_flag and not treatment_suggestion:
        errors["treatment_suggestion"] = "树体状态异常时必须填写处理建议"
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        incisions = db.query(models.Incision).all()
        conditions = ["优秀", "良好", "一般", "较差", "异常"]
        return templates.TemplateResponse("observations/form.html", {
            "request": request, "observation": None, "trees": trees,
            "incisions": incisions, "conditions": conditions,
            "errors": errors, "today": date.today().strftime("%Y-%m-%d"),
            "form_data": {
                "tree_id": tree_id, "observation_date": observation_date,
                "incision_id": incision_id, "tree_condition": tree_condition,
                "bark_healing": bark_healing, "sap_flow": sap_flow,
                "leaf_condition": leaf_condition, "is_abnormal": abnormal_flag,
                "treatment_suggestion": treatment_suggestion,
                "observer": observer, "remarks": remarks
            }
        })
    obs = models.RecoveryObservation(
        tree_id=tree_id, observation_date=o_date, incision_id=incision_id,
        tree_condition=tree_condition, bark_healing=bark_healing, sap_flow=sap_flow,
        leaf_condition=leaf_condition, is_abnormal=abnormal_flag,
        treatment_suggestion=treatment_suggestion, observer=observer, remarks=remarks
    )
    db.add(obs)
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/observations", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/observations/{obs_id}/edit", response_class=HTMLResponse)
async def edit_observation_form(request: Request, obs_id: int, db: Session = Depends(get_db)):
    observation = db.query(models.RecoveryObservation).filter(models.RecoveryObservation.id == obs_id).first()
    if not observation:
        raise HTTPException(status_code=404, detail="观察记录不存在")
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).all()
    conditions = ["优秀", "良好", "一般", "较差", "异常"]
    return templates.TemplateResponse("observations/form.html", {
        "request": request, "observation": observation, "trees": trees,
        "incisions": incisions, "conditions": conditions,
        "errors": {}, "today": date.today().strftime("%Y-%m-%d")
    })


@app.post("/observations/{obs_id}/edit")
async def update_observation(
    request: Request,
    obs_id: int,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    observation_date: str = Form(...),
    incision_id: Optional[int] = Form(None),
    tree_condition: str = Form(...),
    bark_healing: Optional[str] = Form(None),
    sap_flow: Optional[str] = Form(None),
    leaf_condition: Optional[str] = Form(None),
    is_abnormal: Optional[str] = Form(None),
    treatment_suggestion: Optional[str] = Form(None),
    observer: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None)
):
    observation = db.query(models.RecoveryObservation).filter(models.RecoveryObservation.id == obs_id).first()
    if not observation:
        raise HTTPException(status_code=404, detail="观察记录不存在")
    errors = {}
    try:
        o_date = datetime.strptime(observation_date, "%Y-%m-%d").date()
        if o_date > date.today():
            errors["observation_date"] = "观察日期不能晚于当前日期"
    except ValueError:
        errors["observation_date"] = "日期格式不正确"
    abnormal_flag = is_abnormal == "on"
    if abnormal_flag and not treatment_suggestion:
        errors["treatment_suggestion"] = "树体状态异常时必须填写处理建议"
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        incisions = db.query(models.Incision).all()
        conditions = ["优秀", "良好", "一般", "较差", "异常"]
        return templates.TemplateResponse("observations/form.html", {
            "request": request, "observation": observation, "trees": trees,
            "incisions": incisions, "conditions": conditions,
            "errors": errors, "today": date.today().strftime("%Y-%m-%d")
        })
    observation.tree_id = tree_id
    observation.observation_date = o_date
    observation.incision_id = incision_id
    observation.tree_condition = tree_condition
    observation.bark_healing = bark_healing
    observation.sap_flow = sap_flow
    observation.leaf_condition = leaf_condition
    observation.is_abnormal = abnormal_flag
    observation.treatment_suggestion = treatment_suggestion
    observation.observer = observer
    observation.remarks = remarks
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/observations", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/observations/{obs_id}/delete")
async def delete_observation(obs_id: int, db: Session = Depends(get_db)):
    observation = db.query(models.RecoveryObservation).filter(models.RecoveryObservation.id == obs_id).first()
    if not observation:
        raise HTTPException(status_code=404, detail="观察记录不存在")
    db.delete(observation)
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/observations", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/api/tree-yield-trend/{tree_id}")
async def get_tree_yield_trend(tree_id: int, db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).filter(models.Incision.tree_id == tree_id).all()
    traces = []
    for inc in incisions:
        harvests = db.query(models.HarvestBatch).filter(
            models.HarvestBatch.incision_id == inc.id
        ).order_by(models.HarvestBatch.harvest_date).all()
        dates = [h.harvest_date.strftime("%Y-%m-%d") for h in harvests]
        yields = [h.yield_amount for h in harvests]
        traces.append({
            "name": f"{inc.incision_code}({inc.method})",
            "x": dates,
            "y": yields,
            "type": "scatter",
            "mode": "lines+markers"
        })
    tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == tree_id).first()
    return JSONResponse({
        "title": f"漆树 {tree.tree_code if tree else '未知'} 出漆量趋势",
        "traces": traces
    })


@app.get("/api/method-comparison")
async def get_method_comparison(db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).all()
    method_stats = {}
    for inc in incisions:
        m = inc.method
        if m not in method_stats:
            method_stats[m] = {"total_yield": 0, "total_harvests": 0, "count": 0, "incisions": []}
        method_stats[m]["total_yield"] += inc.total_yield
        method_stats[m]["total_harvests"] += inc.total_harvests
        method_stats[m]["count"] += 1
        method_stats[m]["incisions"].append(inc.id)
    methods = list(method_stats.keys())
    avg_yields = []
    total_yields = []
    harvest_counts = []
    abnormal_rates = []
    for m in methods:
        s = method_stats[m]
        avg = s["total_yield"] / s["total_harvests"] if s["total_harvests"] > 0 else 0
        avg_yields.append(round(avg, 2))
        total_yields.append(round(s["total_yield"], 2))
        harvest_counts.append(s["total_harvests"])
        total_obs = db.query(func.count(models.RecoveryObservation.id)).join(
            models.Incision, models.RecoveryObservation.incision_id == models.Incision.id
        ).filter(models.Incision.method == m).scalar() or 0
        abnormal_obs = db.query(func.count(models.RecoveryObservation.id)).join(
            models.Incision, models.RecoveryObservation.incision_id == models.Incision.id
        ).filter(
            models.Incision.method == m, models.RecoveryObservation.is_abnormal == True
        ).scalar() or 0
        rate = round(abnormal_obs / total_obs * 100, 1) if total_obs > 0 else 0
        abnormal_rates.append(rate)
    return JSONResponse({
        "methods": methods,
        "avg_yields": avg_yields,
        "total_yields": total_yields,
        "harvest_counts": harvest_counts,
        "abnormal_rates": abnormal_rates
    })


@app.get("/charts", response_class=HTMLResponse)
async def charts_page(request: Request, db: Session = Depends(get_db)):
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    return templates.TemplateResponse("charts.html", {"request": request, "trees": trees})


@app.get("/plans", response_class=HTMLResponse)
async def list_plans(request: Request, db: Session = Depends(get_db)):
    recalculate_all_reminders(db)
    plans = db.query(models.HarvestPlan).order_by(models.HarvestPlan.plan_date.desc()).all()
    today = date.today()
    return templates.TemplateResponse("plans/list.html", {"request": request, "plans": plans, "today": today})


@app.get("/plans/new", response_class=HTMLResponse)
async def new_plan_form(request: Request, db: Session = Depends(get_db)):
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).filter(models.Incision.status == "活跃").all()
    methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
    today = date.today().strftime("%Y-%m-%d")
    return templates.TemplateResponse("plans/form.html", {
        "request": request, "plan": None, "trees": trees, "incisions": incisions,
        "methods": methods, "errors": {}, "today": today
    })


@app.post("/plans/new")
async def create_plan(
    request: Request,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    incision_id: int = Form(...),
    plan_date: str = Form(...),
    harvest_method: str = Form(...),
    person_in_charge: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    try:
        p_date = datetime.strptime(plan_date, "%Y-%m-%d").date()
        if p_date < date.today():
            errors["plan_date"] = "计划日期不能早于当前日期"
    except ValueError:
        errors["plan_date"] = "日期格式不正确"
    if "plan_date" not in errors:
        ok, msg = check_recovery_period_for_plan(db, incision_id, p_date)
        if not ok:
            errors["recovery"] = msg
        ok2, msg2 = check_abnormal_status(db, incision_id)
        if not ok2:
            errors["abnormal"] = msg2
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        incisions = db.query(models.Incision).filter(models.Incision.status == "活跃").all()
        methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
        return templates.TemplateResponse("plans/form.html", {
            "request": request, "plan": None, "trees": trees, "incisions": incisions,
            "methods": methods, "errors": errors, "today": date.today().strftime("%Y-%m-%d"),
            "form_data": {
                "tree_id": tree_id, "incision_id": incision_id, "plan_date": plan_date,
                "harvest_method": harvest_method, "person_in_charge": person_in_charge, "remarks": remarks
            }
        })
    plan = models.HarvestPlan(
        tree_id=tree_id, incision_id=incision_id, plan_date=p_date,
        harvest_method=harvest_method, person_in_charge=person_in_charge,
        status="待执行", remarks=remarks
    )
    db.add(plan)
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/plans", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/plans/{plan_id}/edit", response_class=HTMLResponse)
async def edit_plan_form(request: Request, plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(models.HarvestPlan).filter(models.HarvestPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="采收计划不存在")
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).all()
    methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
    statuses = ["待执行", "执行中", "已执行", "已过期", "已取消"]
    return templates.TemplateResponse("plans/form.html", {
        "request": request, "plan": plan, "trees": trees, "incisions": incisions,
        "methods": methods, "statuses": statuses, "errors": {}, "today": date.today().strftime("%Y-%m-%d")
    })


@app.post("/plans/{plan_id}/edit")
async def update_plan(
    request: Request,
    plan_id: int,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    incision_id: int = Form(...),
    plan_date: str = Form(...),
    harvest_method: str = Form(...),
    person_in_charge: Optional[str] = Form(None),
    status: str = Form("待执行"),
    remarks: Optional[str] = Form(None)
):
    plan = db.query(models.HarvestPlan).filter(models.HarvestPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="采收计划不存在")
    errors = {}
    try:
        p_date = datetime.strptime(plan_date, "%Y-%m-%d").date()
    except ValueError:
        errors["plan_date"] = "日期格式不正确"
    if "plan_date" not in errors and status == "待执行":
        ok, msg = check_recovery_period_for_plan(db, incision_id, p_date)
        if not ok:
            errors["recovery"] = msg
        ok2, msg2 = check_abnormal_status(db, incision_id)
        if not ok2:
            errors["abnormal"] = msg2
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        incisions = db.query(models.Incision).all()
        methods = ["V字形割法", "一字形割法", "斜线割法", "弧形割法", "其他"]
        statuses = ["待执行", "执行中", "已执行", "已过期", "已取消"]
        return templates.TemplateResponse("plans/form.html", {
            "request": request, "plan": plan, "trees": trees, "incisions": incisions,
            "methods": methods, "statuses": statuses, "errors": errors, "today": date.today().strftime("%Y-%m-%d")
        })
    plan.tree_id = tree_id
    plan.incision_id = incision_id
    plan.plan_date = p_date
    plan.harvest_method = harvest_method
    plan.person_in_charge = person_in_charge
    plan.status = status
    plan.remarks = remarks
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/plans", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/plans/{plan_id}/delete")
async def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(models.HarvestPlan).filter(models.HarvestPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="采收计划不存在")
    db.delete(plan)
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/plans", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/plans/{plan_id}/execute")
async def execute_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(models.HarvestPlan).filter(models.HarvestPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="采收计划不存在")
    plan.status = "执行中"
    db.commit()
    return RedirectResponse(f"/harvests/new?plan_id={plan_id}", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/api/plan-vs-actual")
async def get_plan_vs_actual(db: Session = Depends(get_db)):
    from datetime import datetime as dt
    plans = db.query(models.HarvestPlan).filter(
        models.HarvestPlan.status.in_(["待执行", "执行中", "已执行", "已过期"])
    ).all()
    monthly_data = {}
    for plan in plans:
        month_key = plan.plan_date.strftime("%Y-%m")
        if month_key not in monthly_data:
            monthly_data[month_key] = {"planned": 0, "actual": 0, "plan_count": 0, "actual_count": 0}
        monthly_data[month_key]["planned"] += 1
        monthly_data[month_key]["plan_count"] += 1
        if plan.status == "已执行" and plan.actual_harvest_id:
            harvest = db.query(models.HarvestBatch).filter(
                models.HarvestBatch.id == plan.actual_harvest_id
            ).first()
            if harvest:
                monthly_data[month_key]["actual"] += harvest.yield_amount
                monthly_data[month_key]["actual_count"] += 1
    months = sorted(monthly_data.keys())
    planned_counts = [monthly_data[m]["plan_count"] for m in months]
    actual_counts = [monthly_data[m]["actual_count"] for m in months]
    actual_yields = [round(monthly_data[m]["actual"], 2) for m in months]
    return JSONResponse({
        "months": months,
        "planned_counts": planned_counts,
        "actual_counts": actual_counts,
        "actual_yields": actual_yields
    })


@app.get("/api/abnormal-yield-relation")
async def get_abnormal_yield_relation(db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).all()
    result = []
    for inc in incisions:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == inc.tree_id).first()
        abnormal_obs = db.query(func.count(models.RecoveryObservation.id)).filter(
            models.RecoveryObservation.incision_id == inc.id,
            models.RecoveryObservation.is_abnormal == True
        ).scalar() or 0
        total_obs = db.query(func.count(models.RecoveryObservation.id)).filter(
            models.RecoveryObservation.incision_id == inc.id
        ).scalar() or 0
        abnormal_rate = round(abnormal_obs / total_obs * 100, 1) if total_obs > 0 else 0
        result.append({
            "incision_code": inc.incision_code,
            "tree_code": tree.tree_code if tree else "未知",
            "total_harvests": inc.total_harvests,
            "avg_yield": round(inc.avg_yield, 3),
            "total_yield": round(inc.total_yield, 2),
            "abnormal_count": abnormal_obs,
            "abnormal_rate": abnormal_rate
        })
    result.sort(key=lambda x: x["abnormal_rate"], reverse=True)
    return JSONResponse({
        "incision_codes": [r["incision_code"] for r in result],
        "avg_yields": [r["avg_yield"] for r in result],
        "abnormal_rates": [r["abnormal_rate"] for r in result],
        "total_yields": [r["total_yield"] for r in result],
        "details": result
    })


@app.get("/api/incision-options/{tree_id}")
async def get_incision_options(tree_id: int, db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).filter(
        models.Incision.tree_id == tree_id,
        models.Incision.status == "活跃"
    ).all()
    tree_abnormal_obs = db.query(models.RecoveryObservation).filter(
        models.RecoveryObservation.tree_id == tree_id,
        models.RecoveryObservation.incision_id == None,
        models.RecoveryObservation.is_abnormal == True
    ).order_by(models.RecoveryObservation.observation_date.desc()).first()
    has_tree_abnormal = tree_abnormal_obs is not None
    result = []
    for inc in incisions:
        ok_abnormal, _ = check_abnormal_status(db, inc.id)
        is_abnormal = not ok_abnormal
        result.append({
            "id": inc.id,
            "incision_code": inc.incision_code,
            "method": inc.method,
            "is_abnormal": is_abnormal
        })
    return JSONResponse({"incisions": result, "has_tree_abnormal": has_tree_abnormal})


@app.get("/maintenance", response_class=HTMLResponse)
async def list_maintenance(
    request: Request,
    db: Session = Depends(get_db),
    tree_id: Optional[int] = None,
    incision_id: Optional[int] = None,
    project_type: Optional[str] = None,
    batch_no: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    query = db.query(models.MaintenanceRecord)
    if tree_id:
        query = query.filter(models.MaintenanceRecord.tree_id == tree_id)
    if incision_id:
        query = query.filter(models.MaintenanceRecord.incision_id == incision_id)
    if project_type:
        query = query.filter(models.MaintenanceRecord.project_type == project_type)
    if batch_no:
        query = query.filter(models.MaintenanceRecord.batch_no.like(f"%{batch_no}%"))
    if start_date:
        try:
            s_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            query = query.filter(models.MaintenanceRecord.maintenance_date >= s_date)
        except ValueError:
            pass
    if end_date:
        try:
            e_date = datetime.strptime(end_date, "%Y-%m-%d").date()
            query = query.filter(models.MaintenanceRecord.maintenance_date <= e_date)
        except ValueError:
            pass
    records = query.order_by(models.MaintenanceRecord.maintenance_date.desc()).all()
    total_cost = sum(r.total_cost for r in records)
    total_labor_hours = sum(r.labor_hours for r in records)
    total_labor_cost = sum(r.labor_cost for r in records)
    total_material_cost = sum(r.total_cost - r.labor_cost for r in records)
    cost_by_type = {}
    labor_by_type = {}
    for r in records:
        cost_by_type[r.project_type] = cost_by_type.get(r.project_type, 0) + r.total_cost
        labor_by_type[r.project_type] = labor_by_type.get(r.project_type, 0) + r.labor_hours

    filtered_tree_ids = list(set(r.tree_id for r in records))
    filtered_incision_ids = list(set(r.incision_id for r in records if r.incision_id))
    yield_query = db.query(models.HarvestBatch)
    if tree_id:
        yield_query = yield_query.filter(models.HarvestBatch.tree_id == tree_id)
    if incision_id:
        yield_query = yield_query.filter(models.HarvestBatch.incision_id == incision_id)
    if start_date:
        try:
            s_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            yield_query = yield_query.filter(models.HarvestBatch.harvest_date >= s_date)
        except ValueError:
            pass
    if end_date:
        try:
            e_date = datetime.strptime(end_date, "%Y-%m-%d").date()
            yield_query = yield_query.filter(models.HarvestBatch.harvest_date <= e_date)
        except ValueError:
            pass
    if not tree_id and not incision_id and filtered_tree_ids:
        yield_query = yield_query.filter(models.HarvestBatch.tree_id.in_(filtered_tree_ids))
    total_yield = yield_query.with_entities(func.sum(models.HarvestBatch.yield_amount)).scalar() or 0.0
    unit_cost = round(total_cost / total_yield, 2) if total_yield > 0 else None
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).all()
    project_types = ["施肥", "病虫处理", "树皮养护", "工具消耗", "人工工时"]
    return templates.TemplateResponse("maintenance/list.html", {
        "request": request, "records": records,
        "trees": trees, "incisions": incisions, "project_types": project_types,
        "filters": {
            "tree_id": tree_id, "incision_id": incision_id,
            "project_type": project_type, "batch_no": batch_no,
            "start_date": start_date, "end_date": end_date
        },
        "stats": {
            "total_cost": round(total_cost, 2),
            "total_labor_hours": round(total_labor_hours, 1),
            "total_labor_cost": round(total_labor_cost, 2),
            "total_material_cost": round(total_material_cost, 2),
            "total_yield": round(total_yield, 2),
            "unit_cost": unit_cost,
            "record_count": len(records),
            "cost_by_type": {k: round(v, 2) for k, v in cost_by_type.items()},
            "labor_by_type": {k: round(v, 1) for k, v in labor_by_type.items()}
        }
    })


@app.get("/maintenance/new", response_class=HTMLResponse)
async def new_maintenance_form(request: Request, db: Session = Depends(get_db)):
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).all()
    project_types = ["施肥", "病虫处理", "树皮养护", "工具消耗", "人工工时"]
    today = date.today().strftime("%Y-%m-%d")
    latest_records = db.query(models.MaintenanceRecord).order_by(models.MaintenanceRecord.id.desc()).limit(5).all()
    batch_list = list(set(r.batch_no for r in latest_records if r.batch_no))
    return templates.TemplateResponse("maintenance/form.html", {
        "request": request, "record": None, "trees": trees,
        "incisions": incisions, "project_types": project_types,
        "errors": {}, "today": today, "batch_list": batch_list
    })


@app.post("/maintenance/new")
async def create_maintenance(
    request: Request,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    maintenance_date: str = Form(...),
    project_type: str = Form(...),
    batch_no: Optional[str] = Form(None),
    incision_id: Optional[int] = Form(None),
    quantity: Optional[float] = Form(0),
    unit: Optional[str] = Form(None),
    unit_price: Optional[float] = Form(0),
    total_cost: Optional[float] = Form(0),
    labor_hours: Optional[float] = Form(0),
    labor_cost_rate: Optional[float] = Form(0),
    labor_cost: Optional[float] = Form(0),
    person_in_charge: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    try:
        m_date = datetime.strptime(maintenance_date, "%Y-%m-%d").date()
        if m_date > date.today():
            errors["maintenance_date"] = "养护日期不能晚于当前日期"
    except ValueError:
        errors["maintenance_date"] = "日期格式不正确"
    if quantity is not None and quantity < 0:
        errors["quantity"] = "数量不能为负数"
    if unit_price is not None and unit_price < 0:
        errors["unit_price"] = "单价不能为负数"
    if labor_hours is not None and labor_hours < 0:
        errors["labor_hours"] = "人工工时不能为负数"
    if labor_cost_rate is not None and labor_cost_rate < 0:
        errors["labor_cost_rate"] = "人工费率不能为负数"
    material_cost = (quantity or 0) * (unit_price or 0)
    calc_labor_cost = (labor_hours or 0) * (labor_cost_rate or 0)
    calc_total_cost = material_cost + calc_labor_cost
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        incisions = db.query(models.Incision).all()
        project_types = ["施肥", "病虫处理", "树皮养护", "工具消耗", "人工工时"]
        latest_records = db.query(models.MaintenanceRecord).order_by(models.MaintenanceRecord.id.desc()).limit(5).all()
        batch_list = list(set(r.batch_no for r in latest_records if r.batch_no))
        return templates.TemplateResponse("maintenance/form.html", {
            "request": request, "record": None, "trees": trees,
            "incisions": incisions, "project_types": project_types,
            "errors": errors, "today": date.today().strftime("%Y-%m-%d"),
            "batch_list": batch_list,
            "form_data": {
                "tree_id": tree_id, "maintenance_date": maintenance_date,
                "project_type": project_type, "batch_no": batch_no or "",
                "incision_id": incision_id,
                "quantity": quantity, "unit": unit or "", "unit_price": unit_price,
                "total_cost": total_cost, "labor_hours": labor_hours,
                "labor_cost_rate": labor_cost_rate, "labor_cost": labor_cost,
                "person_in_charge": person_in_charge or "",
                "remarks": remarks or ""
            }
        })
    record = models.MaintenanceRecord(
        tree_id=tree_id, incision_id=incision_id,
        maintenance_date=m_date, project_type=project_type, batch_no=batch_no,
        quantity=quantity or 0, unit=unit,
        unit_price=unit_price or 0, total_cost=calc_total_cost,
        labor_hours=labor_hours or 0, labor_cost_rate=labor_cost_rate or 0,
        labor_cost=calc_labor_cost, person_in_charge=person_in_charge,
        remarks=remarks
    )
    db.add(record)
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/maintenance", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/maintenance/{record_id}/edit", response_class=HTMLResponse)
async def edit_maintenance_form(request: Request, record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.MaintenanceRecord).filter(models.MaintenanceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="养护记录不存在")
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).all()
    project_types = ["施肥", "病虫处理", "树皮养护", "工具消耗", "人工工时"]
    latest_records = db.query(models.MaintenanceRecord).order_by(models.MaintenanceRecord.id.desc()).limit(5).all()
    batch_list = list(set(r.batch_no for r in latest_records if r.batch_no))
    if record.batch_no and record.batch_no not in batch_list:
        batch_list.append(record.batch_no)
    return templates.TemplateResponse("maintenance/form.html", {
        "request": request, "record": record, "trees": trees,
        "incisions": incisions, "project_types": project_types,
        "errors": {}, "today": date.today().strftime("%Y-%m-%d"), "batch_list": batch_list
    })


@app.post("/maintenance/{record_id}/edit")
async def update_maintenance(
    request: Request,
    record_id: int,
    db: Session = Depends(get_db),
    tree_id: int = Form(...),
    maintenance_date: str = Form(...),
    project_type: str = Form(...),
    batch_no: Optional[str] = Form(None),
    incision_id: Optional[int] = Form(None),
    quantity: Optional[float] = Form(0),
    unit: Optional[str] = Form(None),
    unit_price: Optional[float] = Form(0),
    total_cost: Optional[float] = Form(0),
    labor_hours: Optional[float] = Form(0),
    labor_cost_rate: Optional[float] = Form(0),
    labor_cost: Optional[float] = Form(0),
    person_in_charge: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None)
):
    record = db.query(models.MaintenanceRecord).filter(models.MaintenanceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="养护记录不存在")
    errors = {}
    try:
        m_date = datetime.strptime(maintenance_date, "%Y-%m-%d").date()
        if m_date > date.today():
            errors["maintenance_date"] = "养护日期不能晚于当前日期"
    except ValueError:
        errors["maintenance_date"] = "日期格式不正确"
    if quantity is not None and quantity < 0:
        errors["quantity"] = "数量不能为负数"
    if unit_price is not None and unit_price < 0:
        errors["unit_price"] = "单价不能为负数"
    if labor_hours is not None and labor_hours < 0:
        errors["labor_hours"] = "人工工时不能为负数"
    if labor_cost_rate is not None and labor_cost_rate < 0:
        errors["labor_cost_rate"] = "人工费率不能为负数"
    material_cost = (quantity or 0) * (unit_price or 0)
    calc_labor_cost = (labor_hours or 0) * (labor_cost_rate or 0)
    calc_total_cost = material_cost + calc_labor_cost
    if errors:
        trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
        incisions = db.query(models.Incision).all()
        project_types = ["施肥", "病虫处理", "树皮养护", "工具消耗", "人工工时"]
        latest_records = db.query(models.MaintenanceRecord).order_by(models.MaintenanceRecord.id.desc()).limit(5).all()
        batch_list = list(set(r.batch_no for r in latest_records if r.batch_no))
        if batch_no and batch_no not in batch_list:
            batch_list.append(batch_no)
        return templates.TemplateResponse("maintenance/form.html", {
            "request": request, "record": record, "trees": trees,
            "incisions": incisions, "project_types": project_types,
            "errors": errors, "today": date.today().strftime("%Y-%m-%d"),
            "batch_list": batch_list,
            "form_data": {
                "tree_id": tree_id, "maintenance_date": maintenance_date,
                "project_type": project_type, "batch_no": batch_no or "",
                "incision_id": incision_id,
                "quantity": quantity, "unit": unit or "", "unit_price": unit_price,
                "total_cost": calc_total_cost, "labor_hours": labor_hours,
                "labor_cost_rate": labor_cost_rate, "labor_cost": calc_labor_cost,
                "person_in_charge": person_in_charge or "",
                "remarks": remarks or ""
            }
        })
    record.tree_id = tree_id
    record.incision_id = incision_id
    record.maintenance_date = m_date
    record.project_type = project_type
    record.batch_no = batch_no
    record.quantity = quantity or 0
    record.unit = unit
    record.unit_price = unit_price or 0
    record.total_cost = calc_total_cost
    record.labor_hours = labor_hours or 0
    record.labor_cost_rate = labor_cost_rate or 0
    record.labor_cost = calc_labor_cost
    record.person_in_charge = person_in_charge
    record.remarks = remarks
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/maintenance", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/maintenance/{record_id}/delete")
async def delete_maintenance(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.MaintenanceRecord).filter(models.MaintenanceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="养护记录不存在")
    db.delete(record)
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse("/maintenance", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/api/cost-stats/tree/{tree_id}")
async def get_tree_cost_stats(tree_id: int, db: Session = Depends(get_db)):
    tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == tree_id).first()
    if not tree:
        raise HTTPException(status_code=404, detail="漆树不存在")
    records = db.query(models.MaintenanceRecord).filter(
        models.MaintenanceRecord.tree_id == tree_id
    ).all()
    total_cost = sum(r.total_cost for r in records)
    total_labor = sum(r.labor_hours for r in records)
    incisions = db.query(models.Incision).filter(models.Incision.tree_id == tree_id).all()
    total_yield = sum(inc.total_yield for inc in incisions)
    unit_cost = round(total_cost / total_yield, 2) if total_yield > 0 else None
    cost_by_type = {}
    labor_by_type = {}
    for r in records:
        cost_by_type[r.project_type] = cost_by_type.get(r.project_type, 0) + r.total_cost
        labor_by_type[r.project_type] = labor_by_type.get(r.project_type, 0) + r.labor_hours
    return JSONResponse({
        "tree_code": tree.tree_code,
        "total_cost": round(total_cost, 2),
        "total_labor": round(total_labor, 2),
        "total_yield": round(total_yield, 2),
        "unit_cost": unit_cost,
        "cost_by_type": {k: round(v, 2) for k, v in cost_by_type.items()},
        "labor_by_type": {k: round(v, 2) for k, v in labor_by_type.items()},
        "record_count": len(records)
    })


@app.get("/api/cost-stats/incision/{incision_id}")
async def get_incision_cost_stats(incision_id: int, db: Session = Depends(get_db)):
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        raise HTTPException(status_code=404, detail="割口不存在")
    tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == incision.tree_id).first()
    records = db.query(models.MaintenanceRecord).filter(
        models.MaintenanceRecord.incision_id == incision_id
    ).all()
    total_cost = sum(r.total_cost for r in records)
    total_labor = sum(r.labor_hours for r in records)
    unit_cost = round(total_cost / incision.total_yield, 2) if incision.total_yield > 0 else None
    cost_by_type = {}
    labor_by_type = {}
    for r in records:
        cost_by_type[r.project_type] = cost_by_type.get(r.project_type, 0) + r.total_cost
        labor_by_type[r.project_type] = labor_by_type.get(r.project_type, 0) + r.labor_hours
    return JSONResponse({
        "incision_code": incision.incision_code,
        "tree_code": tree.tree_code if tree else "未知",
        "total_cost": round(total_cost, 2),
        "total_labor": round(total_labor, 2),
        "total_yield": round(incision.total_yield, 2),
        "unit_cost": unit_cost,
        "cost_by_type": {k: round(v, 2) for k, v in cost_by_type.items()},
        "labor_by_type": {k: round(v, 2) for k, v in labor_by_type.items()},
        "record_count": len(records)
    })


@app.get("/api/cost-stats/period")
async def get_period_cost_stats(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(models.MaintenanceRecord)
    if start_date:
        try:
            s_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            query = query.filter(models.MaintenanceRecord.maintenance_date >= s_date)
        except ValueError:
            pass
    if end_date:
        try:
            e_date = datetime.strptime(end_date, "%Y-%m-%d").date()
            query = query.filter(models.MaintenanceRecord.maintenance_date <= e_date)
        except ValueError:
            pass
    records = query.all()
    total_cost = sum(r.total_cost for r in records)
    total_labor = sum(r.labor_hours for r in records)
    cost_by_type = {}
    labor_by_type = {}
    for r in records:
        cost_by_type[r.project_type] = cost_by_type.get(r.project_type, 0) + r.total_cost
        labor_by_type[r.project_type] = labor_by_type.get(r.project_type, 0) + r.labor_hours
    return JSONResponse({
        "total_cost": round(total_cost, 2),
        "total_labor": round(total_labor, 2),
        "cost_by_type": {k: round(v, 2) for k, v in cost_by_type.items()},
        "labor_by_type": {k: round(v, 2) for k, v in labor_by_type.items()},
        "record_count": len(records)
    })


@app.get("/api/maintenance-yield-analysis")
async def get_maintenance_yield_analysis(db: Session = Depends(get_db)):
    trees = db.query(models.LacquerTree).all()
    result = []
    for tree in trees:
        records = db.query(models.MaintenanceRecord).filter(
            models.MaintenanceRecord.tree_id == tree.id
        ).all()
        total_cost = sum(r.total_cost for r in records)
        total_labor = sum(r.labor_hours for r in records)
        incisions = db.query(models.Incision).filter(models.Incision.tree_id == tree.id).all()
        total_yield = sum(inc.total_yield for inc in incisions)
        cost_by_type = {}
        for r in records:
            cost_by_type[r.project_type] = cost_by_type.get(r.project_type, 0) + r.total_cost
        abnormal_obs = db.query(func.count(models.RecoveryObservation.id)).filter(
            models.RecoveryObservation.tree_id == tree.id,
            models.RecoveryObservation.is_abnormal == True
        ).scalar() or 0
        total_obs = db.query(func.count(models.RecoveryObservation.id)).filter(
            models.RecoveryObservation.tree_id == tree.id
        ).scalar() or 0
        abnormal_rate = round(abnormal_obs / total_obs * 100, 1) if total_obs > 0 else 0
        unit_cost = round(total_cost / total_yield, 2) if total_yield > 0 else None
        result.append({
            "tree_code": tree.tree_code,
            "tree_id": tree.id,
            "total_cost": round(total_cost, 2),
            "total_labor": round(total_labor, 2),
            "total_yield": round(total_yield, 2),
            "unit_cost": unit_cost,
            "abnormal_rate": abnormal_rate,
            "cost_by_type": {k: round(v, 2) for k, v in cost_by_type.items()},
            "record_count": len(records)
        })
    result.sort(key=lambda x: x["total_cost"], reverse=True)
    return JSONResponse({
        "trees": result,
        "tree_codes": [r["tree_code"] for r in result],
        "total_costs": [r["total_cost"] for r in result],
        "total_yields": [r["total_yield"] for r in result],
        "unit_costs": [r["unit_cost"] or 0 for r in result],
        "abnormal_rates": [r["abnormal_rate"] for r in result]
    })


@app.get("/api/maintenance-type-analysis")
async def get_maintenance_type_analysis(db: Session = Depends(get_db)):
    records = db.query(models.MaintenanceRecord).all()
    type_stats = {}
    for r in records:
        if r.project_type not in type_stats:
            type_stats[r.project_type] = {"total_cost": 0, "total_labor": 0, "count": 0, "material_cost": 0, "labor_cost": 0}
        type_stats[r.project_type]["total_cost"] += r.total_cost
        type_stats[r.project_type]["total_labor"] += r.labor_hours
        type_stats[r.project_type]["count"] += 1
        type_stats[r.project_type]["material_cost"] += (r.total_cost - r.labor_cost)
        type_stats[r.project_type]["labor_cost"] += r.labor_cost
    types = list(type_stats.keys())
    costs = [round(type_stats[t]["total_cost"], 2) for t in types]
    labors = [round(type_stats[t]["total_labor"], 2) for t in types]
    counts = [type_stats[t]["count"] for t in types]
    material_costs = [round(type_stats[t]["material_cost"], 2) for t in types]
    labor_costs = [round(type_stats[t]["labor_cost"], 2) for t in types]
    return JSONResponse({
        "types": types,
        "costs": costs,
        "labors": labors,
        "counts": counts,
        "material_costs": material_costs,
        "labor_costs": labor_costs
    })


@app.get("/api/maintenance-batch-analysis")
async def get_maintenance_batch_analysis(db: Session = Depends(get_db)):
    records = db.query(models.MaintenanceRecord).filter(
        models.MaintenanceRecord.batch_no != None,
        models.MaintenanceRecord.batch_no != ""
    ).all()
    batch_stats = {}
    for r in records:
        b = r.batch_no
        if b not in batch_stats:
            batch_stats[b] = {
                "total_cost": 0, "total_labor": 0, "count": 0,
                "trees": set(), "incisions": set(),
                "start_date": r.maintenance_date, "end_date": r.maintenance_date,
                "material_cost": 0, "labor_cost": 0,
                "cost_by_type": {}
            }
        batch_stats[b]["total_cost"] += r.total_cost
        batch_stats[b]["total_labor"] += r.labor_hours
        batch_stats[b]["count"] += 1
        batch_stats[b]["material_cost"] += (r.total_cost - r.labor_cost)
        batch_stats[b]["labor_cost"] += r.labor_cost
        batch_stats[b]["trees"].add(r.tree_id)
        if r.incision_id:
            batch_stats[b]["incisions"].add(r.incision_id)
        batch_stats[b]["start_date"] = min(batch_stats[b]["start_date"], r.maintenance_date)
        batch_stats[b]["end_date"] = max(batch_stats[b]["end_date"], r.maintenance_date)
        batch_stats[b]["cost_by_type"][r.project_type] = batch_stats[b]["cost_by_type"].get(r.project_type, 0) + r.total_cost
    batches = []
    for b, s in batch_stats.items():
        batches.append({
            "batch_no": b,
            "total_cost": round(s["total_cost"], 2),
            "total_labor": round(s["total_labor"], 1),
            "material_cost": round(s["material_cost"], 2),
            "labor_cost": round(s["labor_cost"], 2),
            "record_count": s["count"],
            "tree_count": len(s["trees"]),
            "incision_count": len(s["incisions"]),
            "start_date": s["start_date"].strftime("%Y-%m-%d"),
            "end_date": s["end_date"].strftime("%Y-%m-%d"),
            "cost_by_type": {k: round(v, 2) for k, v in s["cost_by_type"].items()}
        })
    batches.sort(key=lambda x: x["start_date"], reverse=True)
    return JSONResponse({"batches": batches})


@app.get("/api/monthly-cost-trend")
async def get_monthly_cost_trend(db: Session = Depends(get_db)):
    records = db.query(models.MaintenanceRecord).order_by(
        models.MaintenanceRecord.maintenance_date
    ).all()
    monthly_data = {}
    for r in records:
        month_key = r.maintenance_date.strftime("%Y-%m")
        if month_key not in monthly_data:
            monthly_data[month_key] = {
                "total_cost": 0, "total_labor": 0, "count": 0,
                "material_cost": 0, "labor_cost": 0
            }
        monthly_data[month_key]["total_cost"] += r.total_cost
        monthly_data[month_key]["total_labor"] += r.labor_hours
        monthly_data[month_key]["count"] += 1
        monthly_data[month_key]["material_cost"] += (r.total_cost - r.labor_cost)
        monthly_data[month_key]["labor_cost"] += r.labor_cost
    months = sorted(monthly_data.keys())
    total_costs = [round(monthly_data[m]["total_cost"], 2) for m in months]
    material_costs = [round(monthly_data[m]["material_cost"], 2) for m in months]
    labor_costs = [round(monthly_data[m]["labor_cost"], 2) for m in months]
    total_labors = [round(monthly_data[m]["total_labor"], 1) for m in months]
    counts = [monthly_data[m]["count"] for m in months]
    harvest_monthly = {}
    harvests = db.query(models.HarvestBatch).order_by(models.HarvestBatch.harvest_date).all()
    for h in harvests:
        m_key = h.harvest_date.strftime("%Y-%m")
        harvest_monthly[m_key] = harvest_monthly.get(m_key, 0) + h.yield_amount
    yields = [round(harvest_monthly.get(m, 0), 2) for m in months]
    roi = []
    cum_cost = 0
    cum_yield = 0
    for i, m in enumerate(months):
        cum_cost += monthly_data[m]["total_cost"]
        cum_yield += harvest_monthly.get(m, 0)
        if cum_yield > 0:
            roi.append(round(cum_cost / cum_yield, 2))
        else:
            roi.append(0)
    return JSONResponse({
        "months": months,
        "total_costs": total_costs,
        "material_costs": material_costs,
        "labor_costs": labor_costs,
        "total_labors": total_labors,
        "counts": counts,
        "yields": yields,
        "unit_cost_trend": roi
    })


@app.get("/api/method-recovery-impact")
async def get_method_recovery_impact(db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).all()
    result = []
    for inc in incisions:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == inc.tree_id).first()
        maint_records = db.query(models.MaintenanceRecord).filter(
            models.MaintenanceRecord.incision_id == inc.id
        ).all()
        maint_cost = sum(r.total_cost for r in maint_records)
        maint_labor = sum(r.labor_hours for r in maint_records)
        maint_by_type = {}
        for r in maint_records:
            maint_by_type[r.project_type] = maint_by_type.get(r.project_type, 0) + r.total_cost
        obs_records = db.query(models.RecoveryObservation).filter(
            models.RecoveryObservation.incision_id == inc.id
        ).all()
        abnormal_count = sum(1 for o in obs_records if o.is_abnormal)
        total_obs = len(obs_records)
        avg_recovery_quality = 0
        quality_score = {"优秀": 5, "良好": 4, "一般": 3, "较差": 2, "异常": 1}
        if total_obs > 0:
            avg_recovery_quality = round(
                sum(quality_score.get(o.tree_condition, 0) for o in obs_records) / total_obs, 2
            )
        abnormal_rate = round(abnormal_count / total_obs * 100, 1) if total_obs > 0 else 0
        unit_cost = round(maint_cost / inc.total_yield, 2) if inc.total_yield > 0 else None
        result.append({
            "incision_code": inc.incision_code,
            "tree_code": tree.tree_code if tree else "未知",
            "method": inc.method,
            "total_yield": round(inc.total_yield, 2),
            "avg_yield": round(inc.avg_yield, 3),
            "total_harvests": inc.total_harvests,
            "maint_cost": round(maint_cost, 2),
            "maint_labor": round(maint_labor, 1),
            "maint_by_type": {k: round(v, 2) for k, v in maint_by_type.items()},
            "unit_cost": unit_cost,
            "abnormal_rate": abnormal_rate,
            "avg_recovery_quality": avg_recovery_quality,
            "total_observations": total_obs
        })
    method_groups = {}
    for r in result:
        m = r["method"]
        if m not in method_groups:
            method_groups[m] = {"count": 0, "avg_yield": 0, "avg_unit_cost": 0, "avg_abnormal": 0, "avg_quality": 0}
        method_groups[m]["count"] += 1
        method_groups[m]["avg_yield"] += r["avg_yield"]
        method_groups[m]["avg_unit_cost"] += r["unit_cost"] or 0
        method_groups[m]["avg_abnormal"] += r["abnormal_rate"]
        method_groups[m]["avg_quality"] += r["avg_recovery_quality"]
    methods = []
    for m, s in method_groups.items():
        methods.append({
            "method": m,
            "incision_count": s["count"],
            "avg_yield": round(s["avg_yield"] / s["count"], 3),
            "avg_unit_cost": round(s["avg_unit_cost"] / s["count"], 2),
            "avg_abnormal_rate": round(s["avg_abnormal"] / s["count"], 1),
            "avg_recovery_quality": round(s["avg_quality"] / s["count"], 2)
        })
    return JSONResponse({"details": result, "method_summary": methods})


@app.get("/cost-analysis", response_class=HTMLResponse)
async def cost_analysis_page(request: Request, db: Session = Depends(get_db)):
    total_cost = db.query(func.sum(models.MaintenanceRecord.total_cost)).scalar() or 0.0
    total_labor = db.query(func.sum(models.MaintenanceRecord.labor_hours)).scalar() or 0.0
    total_labor_cost = db.query(func.sum(models.MaintenanceRecord.labor_cost)).scalar() or 0.0
    total_material_cost = total_cost - total_labor_cost
    total_yield = db.query(func.sum(models.HarvestBatch.yield_amount)).scalar() or 0.0
    unit_cost = round(total_cost / total_yield, 2) if total_yield > 0 else None
    record_count = db.query(func.count(models.MaintenanceRecord.id)).scalar() or 0
    avg_labor_rate = round(total_labor_cost / total_labor, 2) if total_labor > 0 else 0
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    incisions = db.query(models.Incision).all()
    project_types = ["施肥", "病虫处理", "树皮养护", "工具消耗", "人工工时"]
    cost_warnings = get_cost_warnings(db)
    return templates.TemplateResponse("cost_analysis.html", {
        "request": request,
        "summary": {
            "total_cost": round(total_cost, 2),
            "total_labor": round(total_labor, 1),
            "total_labor_cost": round(total_labor_cost, 2),
            "total_material_cost": round(total_material_cost, 2),
            "total_yield": round(total_yield, 2),
            "unit_cost": unit_cost,
            "record_count": record_count,
            "avg_labor_rate": avg_labor_rate
        },
        "trees": trees, "incisions": incisions, "project_types": project_types,
        "cost_warnings": cost_warnings
    })
