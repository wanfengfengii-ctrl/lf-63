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
    last_observation = db.query(models.RecoveryObservation).filter(
        models.RecoveryObservation.incision_id == incision_id
    ).order_by(models.RecoveryObservation.observation_date.desc()).first()
    if last_observation and last_observation.is_abnormal:
        return False, f"该割口最近一次恢复观察（{last_observation.observation_date.strftime('%Y-%m-%d')}）为异常状态，需先处理异常后才能计划采收"
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
        "abnormal_warnings": abnormal_warnings[:10]
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
async def new_harvest_form(request: Request, db: Session = Depends(get_db)):
    incisions = db.query(models.Incision).filter(models.Incision.status == "活跃").all()
    weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
    grades = ["特级", "一级", "二级", "三级"]
    today = date.today().strftime("%Y-%m-%d")
    return templates.TemplateResponse("harvests/form.html", {
        "request": request, "harvest": None, "incisions": incisions,
        "weathers": weathers, "grades": grades, "errors": {}, "today": today
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
    remarks: Optional[str] = Form(None)
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
            }
        })
    harvest = models.HarvestBatch(
        incision_id=incision_id, harvest_date=h_date, yield_amount=yield_amount,
        quality_grade=quality_grade, weather_id=weather_id,
        operator=operator, remarks=remarks
    )
    db.add(harvest)
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
    statuses = ["待执行", "已执行", "已过期", "已取消"]
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
        statuses = ["待执行", "已执行", "已过期", "已取消"]
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
    plan.status = "已执行"
    db.commit()
    recalculate_all_reminders(db)
    return RedirectResponse(f"/harvests/new?plan_id={plan_id}", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/api/plan-vs-actual")
async def get_plan_vs_actual(db: Session = Depends(get_db)):
    from datetime import datetime as dt
    plans = db.query(models.HarvestPlan).filter(
        models.HarvestPlan.status.in_(["待执行", "已执行", "已过期"])
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
    return JSONResponse({"incisions": result})
