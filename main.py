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
    tree_count = db.query(func.count(models.LacquerTree.id)).scalar() or 0
    incision_count = db.query(func.count(models.Incision.id)).scalar() or 0
    harvest_count = db.query(func.count(models.HarvestBatch.id)).scalar() or 0
    total_yield = db.query(func.sum(models.HarvestBatch.yield_amount)).scalar() or 0.0
    abnormal_count = db.query(func.count(models.RecoveryObservation.id)).filter(
        models.RecoveryObservation.is_abnormal == True
    ).scalar() or 0
    harvest_reminders = get_next_harvest_dates(db)
    ready_count = sum(1 for r in harvest_reminders if r["status"] == "待采收")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "tree_count": tree_count,
        "incision_count": incision_count,
        "harvest_count": harvest_count,
        "total_yield": round(total_yield, 2),
        "abnormal_count": abnormal_count,
        "harvest_reminders": harvest_reminders[:10],
        "ready_count": ready_count
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
    return RedirectResponse("/incisions", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/incisions/{incision_id}/delete")
async def delete_incision(incision_id: int, db: Session = Depends(get_db)):
    incision = db.query(models.Incision).filter(models.Incision.id == incision_id).first()
    if not incision:
        raise HTTPException(status_code=404, detail="割口记录不存在")
    db.delete(incision)
    db.commit()
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
            "request": request, "weather": weather, "errors": {},
            "today": date.today().strftime("%Y-%m-%d"), "weather_types": weather_types
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
    return RedirectResponse("/observations", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/observations/{obs_id}/delete")
async def delete_observation(obs_id: int, db: Session = Depends(get_db)):
    observation = db.query(models.RecoveryObservation).filter(models.RecoveryObservation.id == obs_id).first()
    if not observation:
        raise HTTPException(status_code=404, detail="观察记录不存在")
    db.delete(observation)
    db.commit()
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
