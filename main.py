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
    recalculate_maintenance_evaluation(db)
    generate_seasonal_recommendations(db)
    recalculate_seasonal_comparisons(db)
    recalculate_quality_analysis(db)


def recalculate_quality_analysis(db: Session):
    harvests = db.query(models.HarvestBatch).all()
    if not harvests:
        return

    analyses = []

    tree_stats = {}
    for h in harvests:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == h.incision.tree_id).first()
        key = tree.tree_code if tree else "未知"
        if key not in tree_stats:
            tree_stats[key] = {"count": 0, "yields": [], "grades": {}, "impurities": [], "moistures": [], "viscosities": []}
        tree_stats[key]["count"] += 1
        tree_stats[key]["yields"].append(h.yield_amount)
        if h.quality_grade:
            tree_stats[key]["grades"][h.quality_grade] = tree_stats[key]["grades"].get(h.quality_grade, 0) + 1
        if h.impurity is not None:
            tree_stats[key]["impurities"].append(h.impurity)
        if h.moisture is not None:
            tree_stats[key]["moistures"].append(h.moisture)
        if h.viscosity is not None:
            tree_stats[key]["viscosities"].append(h.viscosity)

    for key, stats in tree_stats.items():
        analyses.append(build_analysis("tree", key, stats))

    incision_stats = {}
    for h in harvests:
        key = h.incision.incision_code
        if key not in incision_stats:
            incision_stats[key] = {"count": 0, "yields": [], "grades": {}, "impurities": [], "moistures": [], "viscosities": []}
        incision_stats[key]["count"] += 1
        incision_stats[key]["yields"].append(h.yield_amount)
        if h.quality_grade:
            incision_stats[key]["grades"][h.quality_grade] = incision_stats[key]["grades"].get(h.quality_grade, 0) + 1
        if h.impurity is not None:
            incision_stats[key]["impurities"].append(h.impurity)
        if h.moisture is not None:
            incision_stats[key]["moistures"].append(h.moisture)
        if h.viscosity is not None:
            incision_stats[key]["viscosities"].append(h.viscosity)

    for key, stats in incision_stats.items():
        analyses.append(build_analysis("incision", key, stats))

    weather_stats = {}
    for h in harvests:
        if h.weather:
            key = h.weather.weather_type or "未知"
            if key not in weather_stats:
                weather_stats[key] = {"count": 0, "yields": [], "grades": {}, "impurities": [], "moistures": [], "viscosities": []}
            weather_stats[key]["count"] += 1
            weather_stats[key]["yields"].append(h.yield_amount)
            if h.quality_grade:
                weather_stats[key]["grades"][h.quality_grade] = weather_stats[key]["grades"].get(h.quality_grade, 0) + 1
            if h.impurity is not None:
                weather_stats[key]["impurities"].append(h.impurity)
            if h.moisture is not None:
                weather_stats[key]["moistures"].append(h.moisture)
            if h.viscosity is not None:
                weather_stats[key]["viscosities"].append(h.viscosity)

    for key, stats in weather_stats.items():
        analyses.append(build_analysis("weather", key, stats))

    maintenance_stats = {}
    for h in harvests:
        tree_id = h.incision.tree_id
        incision_id = h.incision.id
        start_date = h.harvest_date - timedelta(days=30)
        maint_records = db.query(models.MaintenanceRecord).filter(
            models.MaintenanceRecord.tree_id == tree_id,
            models.MaintenanceRecord.maintenance_date >= start_date,
            models.MaintenanceRecord.maintenance_date <= h.harvest_date
        ).all()
        maint_types = sorted(list(set(r.project_type for r in maint_records))) if maint_records else ["无养护"]
        key = "、".join(maint_types)
        if key not in maintenance_stats:
            maintenance_stats[key] = {"count": 0, "yields": [], "grades": {}, "impurities": [], "moistures": [], "viscosities": []}
        maintenance_stats[key]["count"] += 1
        maintenance_stats[key]["yields"].append(h.yield_amount)
        if h.quality_grade:
            maintenance_stats[key]["grades"][h.quality_grade] = maintenance_stats[key]["grades"].get(h.quality_grade, 0) + 1
        if h.impurity is not None:
            maintenance_stats[key]["impurities"].append(h.impurity)
        if h.moisture is not None:
            maintenance_stats[key]["moistures"].append(h.moisture)
        if h.viscosity is not None:
            maintenance_stats[key]["viscosities"].append(h.viscosity)

    for key, stats in maintenance_stats.items():
        analyses.append(build_analysis("maintenance", key, stats))

    for a_type, a_key, stats in analyses:
        existing = db.query(models.QualityAnalysis).filter(
            models.QualityAnalysis.analysis_type == a_type,
            models.QualityAnalysis.analysis_key == a_key
        ).first()
        if existing:
            existing.total_count = stats["count"]
            existing.avg_yield = stats["avg_yield"]
            existing.grade_counts = stats["grade_counts"]
            existing.high_grade_rate = stats["high_grade_rate"]
            existing.avg_impurity = stats["avg_impurity"]
            existing.avg_moisture = stats["avg_moisture"]
            existing.avg_viscosity = stats["avg_viscosity"]
            existing.details = stats["details"]
        else:
            new_a = models.QualityAnalysis(
                analysis_type=a_type,
                analysis_key=a_key,
                total_count=stats["count"],
                avg_yield=stats["avg_yield"],
                grade_counts=stats["grade_counts"],
                high_grade_rate=stats["high_grade_rate"],
                avg_impurity=stats["avg_impurity"],
                avg_moisture=stats["avg_moisture"],
                avg_viscosity=stats["avg_viscosity"],
                details=stats["details"]
            )
            db.add(new_a)

    db.commit()


def build_analysis(a_type: str, key: str, raw_stats: dict) -> tuple:
    count = raw_stats["count"]
    avg_yield = round(sum(raw_stats["yields"]) / len(raw_stats["yields"]), 3) if raw_stats["yields"] else 0.0
    grade_counts = json.dumps(raw_stats["grades"], ensure_ascii=False)
    high_grade_total = raw_stats["grades"].get("特级", 0) + raw_stats["grades"].get("一级", 0)
    grade_total = sum(raw_stats["grades"].values())
    high_grade_rate = round(high_grade_total / grade_total * 100, 2) if grade_total > 0 else 0.0
    avg_impurity = round(sum(raw_stats["impurities"]) / len(raw_stats["impurities"]), 3) if raw_stats["impurities"] else 0.0
    avg_moisture = round(sum(raw_stats["moistures"]) / len(raw_stats["moistures"]), 3) if raw_stats["moistures"] else 0.0
    avg_viscosity = round(sum(raw_stats["viscosities"]) / len(raw_stats["viscosities"]), 3) if raw_stats["viscosities"] else 0.0
    details = json.dumps({
        "grade_breakdown": raw_stats["grades"]
    }, ensure_ascii=False)
    stats = {
        "count": count,
        "avg_yield": avg_yield,
        "grade_counts": grade_counts,
        "high_grade_rate": high_grade_rate,
        "avg_impurity": avg_impurity,
        "avg_moisture": avg_moisture,
        "avg_viscosity": avg_viscosity,
        "details": details
    }
    return (a_type, key, stats)


def get_quality_warnings(db: Session) -> List[dict]:
    warnings = []
    harvests = db.query(models.HarvestBatch).order_by(
        models.HarvestBatch.harvest_date.desc()
    ).limit(30).all()

    for h in harvests:
        issues = []
        if h.quality_grade in ["三级"]:
            issues.append(f"等级偏低：{h.quality_grade}")
        if h.impurity is not None and h.impurity > 3.0:
            issues.append(f"杂质超标：{h.impurity}%（>3%）")
        if h.moisture is not None and h.moisture > 25.0:
            issues.append(f"水分偏高：{h.moisture}%（>25%）")
        if h.viscosity is not None and h.viscosity < 30:
            issues.append(f"黏度偏低：{h.viscosity}s（<30s）")

        if issues:
            tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == h.incision.tree_id).first()
            warnings.append({
                "harvest_id": h.id,
                "tree_code": tree.tree_code if tree else "未知",
                "incision_code": h.incision.incision_code,
                "harvest_date": h.harvest_date,
                "yield_amount": h.yield_amount,
                "quality_grade": h.quality_grade,
                "issues": issues
            })

    return warnings


def get_inventory_turnover_warnings(db: Session) -> List[dict]:
    warnings = []
    inventories = db.query(models.LacquerInventory).filter(
        models.LacquerInventory.status == "在库"
    ).all()

    today = date.today()
    for inv in inventories:
        days_in_stock = (today - inv.storage_date).days
        turnover_rate = 0.0
        if inv.stock_quantity > 0 and days_in_stock > 0:
            total_sold = sum(s.sale_quantity for s in inv.sales)
            avg_daily_sales = total_sold / days_in_stock if days_in_stock > 0 else 0
            turnover_rate = round(avg_daily_sales / inv.stock_quantity * 100, 2) if inv.stock_quantity > 0 else 0

        if days_in_stock > 90:
            tree = db.query(models.LacquerTree).filter(
                models.LacquerTree.id == inv.harvest.incision.tree_id
            ).first() if inv.harvest else None
            warnings.append({
                "inventory_id": inv.id,
                "batch_no": inv.batch_no,
                "tree_code": tree.tree_code if tree else "未知",
                "storage_date": inv.storage_date,
                "stock_quantity": round(inv.stock_quantity, 2),
                "days_in_stock": days_in_stock,
                "turnover_rate": turnover_rate,
                "storage_location": inv.storage_location or "未指定",
                "person_in_charge": inv.person_in_charge or "未指定"
            })

    warnings.sort(key=lambda x: x["days_in_stock"], reverse=True)
    return warnings[:10]


def get_high_grade_patterns(db: Session) -> dict:
    patterns = {}
    for a_type in ["tree", "incision", "weather", "maintenance"]:
        analyses = db.query(models.QualityAnalysis).filter(
            models.QualityAnalysis.analysis_type == a_type
        ).order_by(models.QualityAnalysis.high_grade_rate.desc()).limit(5).all()
        patterns[a_type] = [{
            "key": a.analysis_key,
            "count": a.total_count,
            "high_grade_rate": a.high_grade_rate,
            "avg_yield": a.avg_yield,
            "avg_impurity": a.avg_impurity,
            "avg_moisture": a.avg_moisture,
            "avg_viscosity": a.avg_viscosity
        } for a in analyses if a.total_count >= 2]

    return patterns


def get_season(dt: date) -> str:
    month = dt.month
    if month in [3, 4, 5]:
        return "春季"
    elif month in [6, 7, 8]:
        return "夏季"
    elif month in [9, 10, 11]:
        return "秋季"
    else:
        return "冬季"


def get_next_season(current_season: str) -> str:
    seasons = ["春季", "夏季", "秋季", "冬季"]
    idx = seasons.index(current_season)
    return seasons[(idx + 1) % 4]


def get_season_date_range(year: int, season: str) -> tuple:
    if season == "春季":
        return (date(year, 3, 1), date(year, 5, 31))
    elif season == "夏季":
        return (date(year, 6, 1), date(year, 8, 31))
    elif season == "秋季":
        return (date(year, 9, 1), date(year, 11, 30))
    else:
        return (date(year, 12, 1), date(year, 2, 28 if year % 4 != 0 else 29))


def calculate_quality_score(condition: str) -> float:
    quality_map = {"优秀": 5.0, "良好": 4.0, "一般": 3.0, "较差": 2.0, "异常": 1.0}
    return quality_map.get(condition, 0.0)


def evaluate_maintenance_for_tree_incision(
    db: Session,
    tree_id: int,
    incision_id: Optional[int],
    year: int,
    season: str,
    batch_no: Optional[str] = None
) -> Optional[models.MaintenanceEvaluation]:
    start_date, end_date = get_season_date_range(year, season)
    
    maintenance_query = db.query(models.MaintenanceRecord).filter(
        models.MaintenanceRecord.tree_id == tree_id,
        models.MaintenanceRecord.maintenance_date >= start_date,
        models.MaintenanceRecord.maintenance_date <= end_date
    )
    
    if incision_id:
        maintenance_query = maintenance_query.filter(models.MaintenanceRecord.incision_id == incision_id)
    else:
        maintenance_query = maintenance_query.filter(models.MaintenanceRecord.incision_id == None)
    
    if batch_no:
        maintenance_query = maintenance_query.filter(models.MaintenanceRecord.batch_no == batch_no)
    
    maintenance_records = maintenance_query.all()
    
    total_cost = sum(r.total_cost for r in maintenance_records)
    total_labor = sum(r.labor_hours for r in maintenance_records)
    maintenance_types = list(set(r.project_type for r in maintenance_records)) if maintenance_records else []
    
    harvest_query = db.query(models.HarvestBatch).filter(
        models.HarvestBatch.harvest_date >= start_date,
        models.HarvestBatch.harvest_date <= end_date
    )
    
    if incision_id:
        harvest_query = harvest_query.filter(models.HarvestBatch.incision_id == incision_id)
    else:
        tree_incisions = db.query(models.Incision).filter(
            models.Incision.tree_id == tree_id
        ).all()
        incision_ids = [inc.id for inc in tree_incisions]
        harvest_query = harvest_query.filter(models.HarvestBatch.incision_id.in_(incision_ids))
    
    harvests = harvest_query.all()
    total_yield = sum(h.yield_amount for h in harvests)
    harvest_count = len(harvests)
    
    obs_query = db.query(models.RecoveryObservation).filter(
        models.RecoveryObservation.tree_id == tree_id,
        models.RecoveryObservation.observation_date >= start_date,
        models.RecoveryObservation.observation_date <= end_date
    )
    
    if incision_id:
        obs_query = obs_query.filter(models.RecoveryObservation.incision_id == incision_id)
    else:
        obs_query = obs_query.filter(models.RecoveryObservation.incision_id == None)
    
    observations = obs_query.all()
    total_obs = len(observations)
    abnormal_count = sum(1 for o in observations if o.is_abnormal)
    abnormal_rate = round(abnormal_count / total_obs * 100, 2) if total_obs > 0 else 0.0
    
    avg_quality = 0.0
    if total_obs > 0:
        avg_quality = round(
            sum(calculate_quality_score(o.tree_condition) for o in observations) / total_obs,
            2
        )
    
    unit_output_cost = round(total_cost / total_yield, 2) if total_yield > 0 else 0.0
    input_output_ratio = round(total_yield / total_cost, 4) if total_cost > 0 else 0.0
    
    yield_score = 0.0
    if harvest_count > 0:
        avg_yield = total_yield / harvest_count
        if avg_yield >= 0.5:
            yield_score = 25.0
        elif avg_yield >= 0.4:
            yield_score = 20.0
        elif avg_yield >= 0.3:
            yield_score = 15.0
        elif avg_yield >= 0.2:
            yield_score = 10.0
        elif avg_yield >= 0.1:
            yield_score = 5.0
    
    cost_score = 0.0
    if total_yield > 0:
        if unit_output_cost <= 100:
            cost_score = 25.0
        elif unit_output_cost <= 200:
            cost_score = 20.0
        elif unit_output_cost <= 300:
            cost_score = 15.0
        elif unit_output_cost <= 500:
            cost_score = 10.0
        elif unit_output_cost <= 800:
            cost_score = 5.0
    
    quality_score = 0.0
    if avg_quality >= 4.5:
        quality_score = 25.0
    elif avg_quality >= 4.0:
        quality_score = 20.0
    elif avg_quality >= 3.5:
        quality_score = 15.0
    elif avg_quality >= 3.0:
        quality_score = 10.0
    elif avg_quality >= 2.0:
        quality_score = 5.0
    
    abnormal_score = 0.0
    if abnormal_rate <= 5:
        abnormal_score = 25.0
    elif abnormal_rate <= 10:
        abnormal_score = 20.0
    elif abnormal_rate <= 20:
        abnormal_score = 15.0
    elif abnormal_rate <= 30:
        abnormal_score = 10.0
    elif abnormal_rate <= 50:
        abnormal_score = 5.0
    
    overall_score = round(yield_score + cost_score + quality_score + abnormal_score, 2)
    
    if overall_score >= 80:
        efficiency_level = "优秀"
    elif overall_score >= 60:
        efficiency_level = "良好"
    elif overall_score >= 40:
        efficiency_level = "中等"
    elif overall_score >= 20:
        efficiency_level = "较差"
    else:
        efficiency_level = "低效"
    
    is_inefficient = overall_score < 40 or unit_output_cost > 500 or (total_cost > 0 and total_yield == 0)
    
    inefficient_reasons = []
    if unit_output_cost > 500:
        inefficient_reasons.append(f"单位出漆成本过高（{unit_output_cost}元/kg，超过500元/kg预警线）")
    if abnormal_rate > 20:
        inefficient_reasons.append(f"异常率偏高（{abnormal_rate}%）")
    if avg_quality < 3.0:
        inefficient_reasons.append(f"恢复质量较差（{avg_quality}/5分）")
    if total_cost > 0 and total_yield == 0:
        inefficient_reasons.append("有养护投入但无产出")
    if yield_score < 10:
        inefficient_reasons.append("产量表现不佳")
    
    suggestions = []
    if unit_output_cost > 300:
        suggestions.append("建议优化养护成本结构，优先采用高性价比的养护方式")
    if abnormal_rate > 10:
        suggestions.append("建议增加观察频率，及时发现并处理异常情况")
    if avg_quality < 3.5:
        suggestions.append("建议加强树皮养护，提升树体恢复质量")
    if total_labor > 50 and input_output_ratio < 0.01:
        suggestions.append("建议优化人工安排，提高劳动效率")
    
    has_data = len(maintenance_records) > 0 or len(harvests) > 0 or len(observations) > 0
    
    existing_eval = db.query(models.MaintenanceEvaluation).filter(
        models.MaintenanceEvaluation.tree_id == tree_id,
        models.MaintenanceEvaluation.incision_id == incision_id,
        models.MaintenanceEvaluation.year == year,
        models.MaintenanceEvaluation.season == season,
        models.MaintenanceEvaluation.batch_no == batch_no
    ).first()
    
    if not has_data:
        if existing_eval:
            db.delete(existing_eval)
        return None
    
    if existing_eval:
        existing_eval.total_maintenance_cost = total_cost
        existing_eval.total_labor_hours = total_labor
        existing_eval.total_yield = total_yield
        existing_eval.harvest_count = harvest_count
        existing_eval.abnormal_count = abnormal_count
        existing_eval.total_observations = total_obs
        existing_eval.abnormal_rate = abnormal_rate
        existing_eval.avg_recovery_quality = avg_quality
        existing_eval.unit_output_cost = unit_output_cost
        existing_eval.input_output_ratio = input_output_ratio
        existing_eval.yield_score = yield_score
        existing_eval.cost_score = cost_score
        existing_eval.quality_score = quality_score
        existing_eval.abnormal_score = abnormal_score
        existing_eval.overall_score = overall_score
        existing_eval.efficiency_level = efficiency_level
        existing_eval.is_inefficient = is_inefficient
        existing_eval.inefficient_reason = "；".join(inefficient_reasons) if inefficient_reasons else None
        existing_eval.suggestions = "；".join(suggestions) if suggestions else None
        existing_eval.maintenance_type = "、".join(maintenance_types) if maintenance_types else None
        evaluation = existing_eval
    else:
        evaluation = models.MaintenanceEvaluation(
            tree_id=tree_id,
            incision_id=incision_id,
            year=year,
            season=season,
            batch_no=batch_no,
            maintenance_type="、".join(maintenance_types) if maintenance_types else None,
            total_maintenance_cost=total_cost,
            total_labor_hours=total_labor,
            total_yield=total_yield,
            harvest_count=harvest_count,
            abnormal_count=abnormal_count,
            total_observations=total_obs,
            abnormal_rate=abnormal_rate,
            avg_recovery_quality=avg_quality,
            unit_output_cost=unit_output_cost,
            input_output_ratio=input_output_ratio,
            yield_score=yield_score,
            cost_score=cost_score,
            quality_score=quality_score,
            abnormal_score=abnormal_score,
            overall_score=overall_score,
            efficiency_level=efficiency_level,
            is_inefficient=is_inefficient,
            inefficient_reason="；".join(inefficient_reasons) if inefficient_reasons else None,
            suggestions="；".join(suggestions) if suggestions else None
        )
        db.add(evaluation)
    
    db.flush()
    return evaluation


def recalculate_maintenance_evaluation(db: Session):
    today = date.today()
    current_year = today.year
    current_season = get_season(today)
    
    seasons_to_calculate = []
    for y in range(current_year - 2, current_year + 1):
        for s in ["春季", "夏季", "秋季", "冬季"]:
            if y == current_year and s == current_season:
                seasons_to_calculate.append((y, s))
            elif y < current_year:
                seasons_to_calculate.append((y, s))
            elif y == current_year:
                season_order = ["春季", "夏季", "秋季", "冬季"]
                if season_order.index(s) < season_order.index(current_season):
                    seasons_to_calculate.append((y, s))
    
    trees = db.query(models.LacquerTree).all()
    
    for tree in trees:
        for year, season in seasons_to_calculate:
            evaluate_maintenance_for_tree_incision(db, tree.id, None, year, season)
            
            incisions = db.query(models.Incision).filter(
                models.Incision.tree_id == tree.id
            ).all()
            for inc in incisions:
                evaluate_maintenance_for_tree_incision(db, inc.tree_id, inc.id, year, season)
    
    db.commit()


def generate_seasonal_recommendations(db: Session):
    today = date.today()
    current_year = today.year
    current_season = get_season(today)
    next_season = get_next_season(current_season)
    next_year = current_year if next_season != "春季" else current_year + 1
    
    seasons_to_generate = [
        (current_year, current_season),
        (next_year, next_season)
    ]
    
    for year, season in seasons_to_generate:
        start_date, end_date = get_season_date_range(year, season)
        
        historical_evals = db.query(models.MaintenanceEvaluation).filter(
            models.MaintenanceEvaluation.season == season
        ).all()
        
        if not historical_evals:
            continue
        
        fert_cost = 0.0
        pest_cost = 0.0
        bark_cost = 0.0
        fert_labor = 0.0
        pest_labor = 0.0
        bark_labor = 0.0
        
        maintenance_records = db.query(models.MaintenanceRecord).filter(
            models.MaintenanceRecord.maintenance_date >= start_date.replace(year=year-1),
            models.MaintenanceRecord.maintenance_date <= end_date.replace(year=year-1)
        ).all()
        
        for r in maintenance_records:
            if r.project_type == "施肥":
                fert_cost += r.total_cost
                fert_labor += r.labor_hours
            elif r.project_type == "病虫处理":
                pest_cost += r.total_cost
                pest_labor += r.labor_hours
            elif r.project_type == "树皮养护":
                bark_cost += r.total_cost
                bark_labor += r.labor_hours
        
        avg_yield_score = sum(e.yield_score for e in historical_evals) / len(historical_evals)
        avg_cost_score = sum(e.cost_score for e in historical_evals) / len(historical_evals)
        avg_quality_score = sum(e.quality_score for e in historical_evals) / len(historical_evals)
        avg_abnormal_score = sum(e.abnormal_score for e in historical_evals) / len(historical_evals)
        
        fertilization_suggestion = generate_fertilization_suggestion(season, avg_quality_score, fert_cost)
        pest_control_suggestion = generate_pest_control_suggestion(season, avg_abnormal_score, pest_cost)
        bark_care_suggestion = generate_bark_care_suggestion(season, avg_quality_score, bark_cost)
        labor_arrangement_suggestion = generate_labor_suggestion(season, fert_labor + pest_labor + bark_labor)
        
        overall_strategy = generate_overall_strategy(season, avg_yield_score, avg_cost_score)
        key_points = generate_key_points(season, avg_yield_score, avg_quality_score, avg_abnormal_score)
        expected_effect = generate_expected_effect(season, avg_yield_score, avg_quality_score)
        
        estimated_cost = round((fert_cost + pest_cost + bark_cost) * 1.1, 2)
        estimated_labor = round((fert_labor + pest_labor + bark_labor) * 1.1, 1)
        
        existing_rec = db.query(models.SeasonalRecommendation).filter(
            models.SeasonalRecommendation.year == year,
            models.SeasonalRecommendation.season == season
        ).first()
        
        if existing_rec:
            existing_rec.fertilization_suggestion = fertilization_suggestion
            existing_rec.pest_control_suggestion = pest_control_suggestion
            existing_rec.bark_care_suggestion = bark_care_suggestion
            existing_rec.labor_arrangement_suggestion = labor_arrangement_suggestion
            existing_rec.overall_strategy = overall_strategy
            existing_rec.key_points = key_points
            existing_rec.expected_effect = expected_effect
            existing_rec.estimated_cost = estimated_cost
            existing_rec.estimated_labor = estimated_labor
        else:
            new_rec = models.SeasonalRecommendation(
                year=year,
                season=season,
                fertilization_suggestion=fertilization_suggestion,
                pest_control_suggestion=pest_control_suggestion,
                bark_care_suggestion=bark_care_suggestion,
                labor_arrangement_suggestion=labor_arrangement_suggestion,
                overall_strategy=overall_strategy,
                key_points=key_points,
                expected_effect=expected_effect,
                estimated_cost=estimated_cost,
                estimated_labor=estimated_labor
            )
            db.add(new_rec)
    
    db.commit()


def generate_fertilization_suggestion(season: str, quality_score: float, last_cost: float) -> str:
    suggestions = []
    
    if season == "春季":
        suggestions.append("春季是漆树生长旺季，建议在3月初施用腐熟有机肥作为基肥")
        suggestions.append("每株施用量约5-8kg，采用环沟施肥法，深度20-30cm")
        if quality_score < 15:
            suggestions.append("建议增加磷钾肥比例，促进根系发育和树势恢复")
        suggestions.append("4月中下旬可追施一次氮肥，促进新梢生长")
    elif season == "夏季":
        suggestions.append("夏季高温，建议减少施肥量，避免烧根")
        suggestions.append("可采用叶面喷施的方式补充微量元素")
        if last_cost > 500:
            suggestions.append("建议采用缓释肥料，延长肥效，减少施肥频次")
        suggestions.append("注意雨后及时补肥，防止养分流失")
    elif season == "秋季":
        suggestions.append("秋季采收结束后，及时施用采后肥，恢复树势")
        suggestions.append("以有机肥为主，配合适量磷钾肥，增强抗寒能力")
        suggestions.append("每株施用量约8-10kg，为越冬和来年生长储备养分")
    elif season == "冬季":
        suggestions.append("冬季休眠期，建议施用基肥，改良土壤结构")
        suggestions.append("结合冬季清园，深翻土壤，埋入有机肥")
        if quality_score < 15:
            suggestions.append("可适当增加硼肥、锌肥等微量元素，改善树皮质量")
    
    return "；".join(suggestions)


def generate_pest_control_suggestion(season: str, abnormal_score: float, last_cost: float) -> str:
    suggestions = []
    
    if season == "春季":
        suggestions.append("春季是病虫害高发期，需重点防治蚜虫、红蜘蛛")
        suggestions.append("3月中下旬喷施石硫合剂，预防病害发生")
        if abnormal_score < 15:
            suggestions.append("建议增加巡查频率，每7-10天巡查一次")
        suggestions.append("注意观察新梢和叶片，及时发现虫害迹象")
    elif season == "夏季":
        suggestions.append("夏季高温高湿，需重点防治天牛、介壳虫和根腐病")
        suggestions.append("建议采用生物防治为主，化学防治为辅的策略")
        suggestions.append("保持林间通风透光，降低湿度，减少病害发生")
        if last_cost > 300:
            suggestions.append("建议安装诱虫灯，减少农药使用量")
    elif season == "秋季":
        suggestions.append("秋季采收后，及时清理果园，减少病虫源")
        suggestions.append("喷施一次保护性杀菌剂，保护伤口")
        if abnormal_score < 15:
            suggestions.append("建议对异常树体进行重点处理，刮除病斑，涂抹药剂")
        suggestions.append("检查树干，发现蛀孔及时注药防治天牛幼虫")
    elif season == "冬季":
        suggestions.append("冬季清园是全年病虫害防治的关键")
        suggestions.append("清除枯枝落叶，刮除老树皮，集中烧毁")
        suggestions.append("树干涂白，防止冻害和病虫害越冬")
        suggestions.append("喷施5波美度石硫合剂，消灭越冬病虫源")
    
    return "；".join(suggestions)


def generate_bark_care_suggestion(season: str, quality_score: float, last_cost: float) -> str:
    suggestions = []
    
    if season == "春季":
        suggestions.append("春季树皮开始恢复生长，需避免机械损伤")
        suggestions.append("检查割口恢复情况，对愈合不良的割口进行处理")
        if quality_score < 15:
            suggestions.append("建议使用树皮愈合剂涂抹割口，促进愈合")
        suggestions.append("新梢萌发期，注意保护嫩枝，避免风折")
    elif season == "夏季":
        suggestions.append("夏季高温，需防止树皮日灼")
        suggestions.append("可采用树干包草或涂白的方式降低树皮温度")
        suggestions.append("及时清除树干上的寄生植物和苔藓")
        if last_cost > 200:
            suggestions.append("建议推广使用环保型树皮保护剂")
    elif season == "秋季":
        suggestions.append("秋季采收时，注意保护树皮，避免过度切割")
        suggestions.append("采收后及时对割口进行消毒处理")
        if quality_score < 15:
            suggestions.append("建议对割口进行保湿处理，促进愈合")
        suggestions.append("检查树皮损伤情况，及时修复较大伤口")
    elif season == "冬季":
        suggestions.append("冬季树干涂白是保护树皮的重要措施")
        suggestions.append("涂白剂配方：生石灰10份、硫磺1份、食盐0.5份、水40份")
        suggestions.append("涂白高度1.2-1.5米，重点是树干向阳面")
        suggestions.append("注意防止冻害，极端低温天气可采取包裹保温措施")
    
    return "；".join(suggestions)


def generate_labor_suggestion(season: str, total_labor: float) -> str:
    suggestions = []
    
    if season == "春季":
        suggestions.append("春季工作重点：施肥、病虫害防治、新梢管理")
        suggestions.append("建议配备3-5人的专业养护队伍")
        if total_labor > 100:
            suggestions.append("考虑采用机械化作业，提高施肥效率")
        suggestions.append("合理安排工时，避开雨天作业")
    elif season == "夏季":
        suggestions.append("夏季工作重点：防暑降温、病虫害监测、树体巡查")
        suggestions.append("建议调整作业时间，避开中午高温时段")
        suggestions.append("早晚作业，中午安排休息，防止人员中暑")
        suggestions.append("增加临时用工，应对夏季病虫害高发期")
    elif season == "秋季":
        suggestions.append("秋季工作重点：采收、采后养护、清园")
        suggestions.append("采收期需配备充足的熟练采收工人")
        suggestions.append("采后及时安排养护人员进行树体恢复处理")
        if total_labor > 150:
            suggestions.append("建议分组作业，提高采收和养护效率")
    elif season == "冬季":
        suggestions.append("冬季工作重点：清园、修剪、树干涂白、基肥施用")
        suggestions.append("可利用农闲期对养护人员进行技术培训")
        suggestions.append("合理安排冬季作业，避开极端低温天气")
        suggestions.append("做好冬季防火工作")
    
    return "；".join(suggestions)


def generate_overall_strategy(season: str, yield_score: float, cost_score: float) -> str:
    if season == "春季":
        if yield_score < 10 and cost_score < 10:
            return "春季以\"促生长、提产量\"为核心，重点加强施肥和病虫害防治，同时优化成本结构，提高投入产出比"
        elif yield_score < 10:
            return "春季以\"促生长、提产量\"为核心，重点加强施肥和新梢管理，促进漆树健壮生长"
        elif cost_score < 10:
            return "春季以\"控成本、提效率\"为核心，在保证养护质量的前提下，优化施肥方案，降低单位成本"
        return "春季以\"保稳产、提质量\"为核心，维持现有养护水平，重点关注树势恢复和病虫害预防"
    elif season == "夏季":
        if yield_score < 10 and cost_score < 10:
            return "夏季以\"保产能、降成本\"为核心，重点做好防暑降温、病虫害监测，同时优化人工安排，提高作业效率"
        elif yield_score < 10:
            return "夏季以\"保产能、提效率\"为核心，重点做好树体养护，减少高温对产量的影响"
        elif cost_score < 10:
            return "夏季以\"降成本、控风险\"为核心，重点优化农药使用，推广生物防治，降低养护成本"
        return "夏季以\"稳生产、保质量\"为核心，维持正常养护，重点关注极端天气应对"
    elif season == "秋季":
        if yield_score < 10 and cost_score < 10:
            return "秋季以\"提产量、促恢复\"为核心，重点做好采收管理和采后养护，同时优化采收效率，降低单位成本"
        elif yield_score < 10:
            return "秋季以\"提产量、保质量\"为核心，重点做好适时采收，提高采收质量和效率"
        elif cost_score < 10:
            return "秋季以\"促恢复、控成本\"为核心，重点做好采后树势恢复，同时合理控制养护投入"
        return "秋季以\"保采收、促恢复\"为核心，做好采收和采后养护的平衡，为来年生产打好基础"
    elif season == "冬季":
        if yield_score < 10 and cost_score < 10:
            return "冬季以\"强基础、提效益\"为核心，重点做好清园和基肥施用，同时优化冬季作业安排，降低人工成本"
        elif yield_score < 10:
            return "冬季以\"强基础、提树势\"为核心，重点做好基肥施用和树体修剪，增强树势"
        elif cost_score < 10:
            return "冬季以\"提效益、降成本\"为核心，重点优化冬季养护方案，合理控制投入"
        return "冬季以\"养树势、备来年\"为核心，做好清园、修剪、涂白等基础工作，为来年丰产创造条件"


def generate_key_points(season: str, yield_score: float, quality_score: float, abnormal_score: float) -> str:
    points = []
    
    if season == "春季":
        points.append("适时施肥，保证养分供应")
        if abnormal_score < 15:
            points.append("加强病虫害监测与防治")
        if quality_score < 15:
            points.append("做好割口愈合管理")
        points.append("注意倒春寒防护")
    elif season == "夏季":
        points.append("防暑降温，防止日灼")
        if abnormal_score < 15:
            points.append("重点防治天牛、介壳虫")
        if quality_score < 15:
            points.append("加强树皮养护，防止树皮损伤")
        points.append("雨季注意排水防涝")
    elif season == "秋季":
        points.append("适时采收，保证质量")
        if quality_score < 15:
            points.append("采收后及时进行割口处理")
        if abnormal_score < 15:
            points.append("采后清园，减少病虫源")
        points.append("采后施肥，恢复树势")
    elif season == "冬季":
        points.append("彻底清园，消灭越冬病虫")
        points.append("树干涂白，防止冻害")
        if quality_score < 15:
            points.append("刮除老树皮，促进树皮更新")
        points.append("深翻改土，施用基肥")
    
    return "；".join(points)


def generate_expected_effect(season: str, yield_score: float, quality_score: float) -> str:
    effects = []
    
    yield_improvement = "+10%" if yield_score < 15 else "+5%"
    quality_improvement = "提升0.5分" if quality_score < 15 else "维持现有水平"
    abnormal_reduction = "降低10%"
    
    if season == "春季":
        effects.append(f"预计春季产量可提升{yield_improvement}")
        effects.append(f"树体恢复质量{quality_improvement}")
        effects.append(f"病虫害发生率{abnormal_reduction}")
        effects.append("新梢生长健壮，为夏梢培养打好基础")
    elif season == "夏季":
        effects.append(f"预计夏季产量可维持稳定或{yield_improvement}")
        effects.append(f"树皮质量{quality_improvement}")
        effects.append(f"高温危害发生率{abnormal_reduction}")
        effects.append("树势保持健壮，顺利越夏")
    elif season == "秋季":
        effects.append(f"预计秋季采收产量{yield_improvement}")
        effects.append(f"树皮愈合质量{quality_improvement}")
        effects.append("采后树势恢复良好")
        effects.append("为冬季休眠和来年生长储备充足养分")
    elif season == "冬季":
        effects.append("树体安全越冬，无严重冻害")
        effects.append("越冬病虫源显著减少")
        effects.append(f"土壤肥力{quality_improvement}")
        effects.append("来年春季树势健壮，丰产基础好")
    
    return "；".join(effects)


def recalculate_seasonal_comparisons(db: Session):
    today = date.today()
    current_year = today.year
    
    for year in range(current_year - 2, current_year + 1):
        for season in ["春季", "夏季", "秋季", "冬季"]:
            start_date, end_date = get_season_date_range(year, season)
            
            evals = db.query(models.MaintenanceEvaluation).filter(
                models.MaintenanceEvaluation.year == year,
                models.MaintenanceEvaluation.season == season
            ).all()
            
            if not evals:
                continue
            
            total_cost = sum(e.total_maintenance_cost for e in evals)
            total_labor = sum(e.total_labor_hours for e in evals)
            total_yield = sum(e.total_yield for e in evals)
            avg_unit_cost = round(sum(e.unit_output_cost for e in evals) / len(evals), 2) if evals else 0.0
            avg_abnormal_rate = round(sum(e.abnormal_rate for e in evals) / len(evals), 2) if evals else 0.0
            avg_overall_score = round(sum(e.overall_score for e in evals) / len(evals), 2) if evals else 0.0
            
            tree_ids = list(set(e.tree_id for e in evals))
            incision_ids = list(set(e.incision_id for e in evals if e.incision_id))
            
            maintenance_records = db.query(models.MaintenanceRecord).filter(
                models.MaintenanceRecord.maintenance_date >= start_date,
                models.MaintenanceRecord.maintenance_date <= end_date
            ).all()
            
            cost_by_type = {}
            labor_by_type = {}
            for r in maintenance_records:
                cost_by_type[r.project_type] = cost_by_type.get(r.project_type, 0) + r.total_cost
                labor_by_type[r.project_type] = labor_by_type.get(r.project_type, 0) + r.labor_hours
            
            existing_comp = db.query(models.SeasonalComparison).filter(
                models.SeasonalComparison.year == year,
                models.SeasonalComparison.season == season
            ).first()
            
            if existing_comp:
                existing_comp.total_maintenance_cost = total_cost
                existing_comp.total_labor_hours = total_labor
                existing_comp.total_yield = total_yield
                existing_comp.avg_unit_cost = avg_unit_cost
                existing_comp.avg_abnormal_rate = avg_abnormal_rate
                existing_comp.avg_overall_score = avg_overall_score
                existing_comp.tree_count = len(tree_ids)
                existing_comp.incision_count = len(incision_ids)
                existing_comp.cost_by_type = json.dumps(cost_by_type, ensure_ascii=False)
                existing_comp.labor_by_type = json.dumps(labor_by_type, ensure_ascii=False)
            else:
                new_comp = models.SeasonalComparison(
                    year=year,
                    season=season,
                    total_maintenance_cost=total_cost,
                    total_labor_hours=total_labor,
                    total_yield=total_yield,
                    avg_unit_cost=avg_unit_cost,
                    avg_abnormal_rate=avg_abnormal_rate,
                    avg_overall_score=avg_overall_score,
                    tree_count=len(tree_ids),
                    incision_count=len(incision_ids),
                    cost_by_type=json.dumps(cost_by_type, ensure_ascii=False),
                    labor_by_type=json.dumps(labor_by_type, ensure_ascii=False)
                )
                db.add(new_comp)
    
    db.commit()


def get_inefficient_warnings(db: Session) -> List[dict]:
    today = date.today()
    current_year = today.year
    current_season = get_season(today)
    
    inefficient_evals = db.query(models.MaintenanceEvaluation).filter(
        models.MaintenanceEvaluation.is_inefficient == True,
        models.MaintenanceEvaluation.year == current_year,
        models.MaintenanceEvaluation.season == current_season
    ).order_by(models.MaintenanceEvaluation.overall_score).all()
    
    results = []
    for eval in inefficient_evals:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == eval.tree_id).first()
        incision = db.query(models.Incision).filter(models.Incision.id == eval.incision_id).first() if eval.incision_id else None
        results.append({
            "id": eval.id,
            "tree_code": tree.tree_code if tree else "未知",
            "incision_code": incision.incision_code if incision else "整树养护",
            "season": eval.season,
            "overall_score": eval.overall_score,
            "efficiency_level": eval.efficiency_level,
            "total_cost": round(eval.total_maintenance_cost, 2),
            "total_yield": round(eval.total_yield, 2),
            "unit_cost": round(eval.unit_output_cost, 2),
            "abnormal_rate": eval.abnormal_rate,
            "inefficient_reason": eval.inefficient_reason,
            "suggestions": eval.suggestions,
            "type": "incision" if eval.incision_id else "tree"
        })
    
    return results


def get_seasonal_suggestions(db: Session) -> dict:
    today = date.today()
    current_year = today.year
    current_season = get_season(today)
    
    current_rec = db.query(models.SeasonalRecommendation).filter(
        models.SeasonalRecommendation.year == current_year,
        models.SeasonalRecommendation.season == current_season
    ).first()
    
    next_season = get_next_season(current_season)
    next_year = current_year if next_season != "春季" else current_year + 1
    
    next_rec = db.query(models.SeasonalRecommendation).filter(
        models.SeasonalRecommendation.year == next_year,
        models.SeasonalRecommendation.season == next_season
    ).first()
    
    return {
        "current": {
            "season": current_season,
            "year": current_year,
            "recommendation": current_rec
        },
        "next": {
            "season": next_season,
            "year": next_year,
            "recommendation": next_rec
        }
    }


def get_evaluation_scores(db: Session) -> dict:
    today = date.today()
    current_year = today.year
    
    evals = db.query(models.MaintenanceEvaluation).filter(
        models.MaintenanceEvaluation.year == current_year
    ).all()
    
    if not evals:
        evals = db.query(models.MaintenanceEvaluation).filter(
            models.MaintenanceEvaluation.year == current_year - 1
        ).all()
    
    season_scores = {}
    for s in ["春季", "夏季", "秋季", "冬季"]:
        season_evals = [e for e in evals if e.season == s]
        if season_evals:
            season_scores[s] = {
                "avg_overall_score": round(sum(e.overall_score for e in season_evals) / len(season_evals), 2),
                "avg_yield_score": round(sum(e.yield_score for e in season_evals) / len(season_evals), 2),
                "avg_cost_score": round(sum(e.cost_score for e in season_evals) / len(season_evals), 2),
                "avg_quality_score": round(sum(e.quality_score for e in season_evals) / len(season_evals), 2),
                "avg_abnormal_score": round(sum(e.abnormal_score for e in season_evals) / len(season_evals), 2),
                "count": len(season_evals),
                "total_cost": round(sum(e.total_maintenance_cost for e in season_evals), 2),
                "total_yield": round(sum(e.total_yield for e in season_evals), 2),
                "avg_unit_cost": round(sum(e.unit_output_cost for e in season_evals) / len(season_evals), 2),
                "avg_abnormal_rate": round(sum(e.abnormal_rate for e in season_evals) / len(season_evals), 2)
            }
    
    return season_scores


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
    
    seasonal_suggestions = get_seasonal_suggestions(db)
    inefficient_warnings = get_inefficient_warnings(db)
    
    quality_warnings = get_quality_warnings(db)
    inventory_turnover_warnings = get_inventory_turnover_warnings(db)
    
    current_season = get_season(date.today())
    season_emoji = {"春季": "🌸", "夏季": "☀️", "秋季": "🍂", "冬季": "❄️"}.get(current_season, "🌿")
    
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
        "total_labor_hours": round(total_labor_hours, 1),
        "seasonal_suggestions": seasonal_suggestions,
        "inefficient_warnings": inefficient_warnings[:10],
        "current_season": current_season,
        "season_emoji": season_emoji,
        "quality_warnings": quality_warnings[:10],
        "inventory_turnover_warnings": inventory_turnover_warnings[:10]
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
    colors = ["乳白色", "浅褐色", "深褐色", "棕黑色", "其他"]
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
        "weathers": weathers, "grades": grades, "colors": colors,
        "errors": {}, "today": today, "form_data": form_data, "plan_id": plan_id
    })


@app.post("/harvests/new")
async def create_harvest(
    request: Request,
    db: Session = Depends(get_db),
    incision_id: int = Form(...),
    harvest_date: str = Form(...),
    yield_amount: float = Form(...),
    color: Optional[str] = Form(None),
    impurity: Optional[float] = Form(None),
    moisture: Optional[float] = Form(None),
    viscosity: Optional[float] = Form(None),
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
    if impurity is not None and impurity < 0:
        errors["impurity"] = "杂质含量不能为负数"
    if moisture is not None and moisture < 0:
        errors["moisture"] = "水分含量不能为负数"
    if viscosity is not None and viscosity < 0:
        errors["viscosity"] = "黏度不能为负数"
    if "harvest_date" not in errors:
        ok, msg = check_recovery_period(db, incision_id, h_date)
        if not ok:
            errors["recovery"] = msg
    if errors:
        incisions = db.query(models.Incision).filter(models.Incision.status == "活跃").all()
        weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
        grades = ["特级", "一级", "二级", "三级"]
        colors = ["乳白色", "浅褐色", "深褐色", "棕黑色", "其他"]
        return templates.TemplateResponse("harvests/form.html", {
            "request": request, "harvest": None, "incisions": incisions,
            "weathers": weathers, "grades": grades, "colors": colors, "errors": errors,
            "today": date.today().strftime("%Y-%m-%d"),
            "form_data": {
                "incision_id": incision_id, "harvest_date": harvest_date,
                "yield_amount": yield_amount, "color": color,
                "impurity": impurity, "moisture": moisture, "viscosity": viscosity,
                "quality_grade": quality_grade, "weather_id": weather_id,
                "operator": operator, "remarks": remarks
            },
            "plan_id": plan_id
        })
    harvest = models.HarvestBatch(
        incision_id=incision_id, harvest_date=h_date, yield_amount=yield_amount,
        color=color, impurity=impurity, moisture=moisture, viscosity=viscosity,
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
    recalculate_quality_analysis(db)
    return RedirectResponse("/harvests", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/harvests/{harvest_id}/edit", response_class=HTMLResponse)
async def edit_harvest_form(request: Request, harvest_id: int, db: Session = Depends(get_db)):
    harvest = db.query(models.HarvestBatch).filter(models.HarvestBatch.id == harvest_id).first()
    if not harvest:
        raise HTTPException(status_code=404, detail="采收批次不存在")
    incisions = db.query(models.Incision).all()
    weathers = db.query(models.WeatherCondition).order_by(models.WeatherCondition.record_date.desc()).all()
    grades = ["特级", "一级", "二级", "三级"]
    colors = ["乳白色", "浅褐色", "深褐色", "棕黑色", "其他"]
    return templates.TemplateResponse("harvests/form.html", {
        "request": request, "harvest": harvest, "incisions": incisions,
        "weathers": weathers, "grades": grades, "colors": colors, "errors": {},
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
    color: Optional[str] = Form(None),
    impurity: Optional[float] = Form(None),
    moisture: Optional[float] = Form(None),
    viscosity: Optional[float] = Form(None),
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
    if impurity is not None and impurity < 0:
        errors["impurity"] = "杂质含量不能为负数"
    if moisture is not None and moisture < 0:
        errors["moisture"] = "水分含量不能为负数"
    if viscosity is not None and viscosity < 0:
        errors["viscosity"] = "黏度不能为负数"
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
        colors = ["乳白色", "浅褐色", "深褐色", "棕黑色", "其他"]
        return templates.TemplateResponse("harvests/form.html", {
            "request": request, "harvest": harvest, "incisions": incisions,
            "weathers": weathers, "grades": grades, "colors": colors, "errors": errors,
            "today": date.today().strftime("%Y-%m-%d")
        })
    harvest.incision_id = incision_id
    harvest.harvest_date = h_date
    harvest.yield_amount = yield_amount
    harvest.color = color
    harvest.impurity = impurity
    harvest.moisture = moisture
    harvest.viscosity = viscosity
    harvest.quality_grade = quality_grade
    harvest.weather_id = weather_id
    harvest.operator = operator
    harvest.remarks = remarks
    db.commit()
    recalculate_incision_stats(db, old_incision_id)
    if old_incision_id != incision_id:
        recalculate_incision_stats(db, incision_id)
    recalculate_all_reminders(db)
    recalculate_quality_analysis(db)
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
    recalculate_quality_analysis(db)
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
    recalculate_quality_analysis(db)
    return RedirectResponse("/weather", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/weather/{weather_id}/delete")
async def delete_weather(weather_id: int, db: Session = Depends(get_db)):
    weather = db.query(models.WeatherCondition).filter(models.WeatherCondition.id == weather_id).first()
    if not weather:
        raise HTTPException(status_code=404, detail="天气记录不存在")
    db.delete(weather)
    db.commit()
    recalculate_quality_analysis(db)
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
    recalculate_all_reminders(db)
    trees = db.query(models.LacquerTree).order_by(models.LacquerTree.tree_code).all()
    evaluation_scores = get_evaluation_scores(db)
    seasonal_suggestions = get_seasonal_suggestions(db)
    inefficient_warnings = get_inefficient_warnings(db)
    
    seasonal_comparisons = db.query(models.SeasonalComparison).order_by(
        models.SeasonalComparison.year.desc(),
        models.SeasonalComparison.season
    ).all()
    
    current_season = get_season(date.today())
    season_emoji = {"春季": "🌸", "夏季": "☀️", "秋季": "🍂", "冬季": "❄️"}.get(current_season, "🌿")
    
    return templates.TemplateResponse("charts.html", {
        "request": request, 
        "trees": trees,
        "evaluation_scores": evaluation_scores,
        "seasonal_suggestions": seasonal_suggestions,
        "inefficient_warnings": inefficient_warnings,
        "seasonal_comparisons": seasonal_comparisons,
        "current_season": current_season,
        "season_emoji": season_emoji
    })


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
    recalculate_quality_analysis(db)
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
    recalculate_quality_analysis(db)
    return RedirectResponse("/maintenance", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/maintenance/{record_id}/delete")
async def delete_maintenance(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.MaintenanceRecord).filter(models.MaintenanceRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="养护记录不存在")
    db.delete(record)
    db.commit()
    recalculate_all_reminders(db)
    recalculate_quality_analysis(db)
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


@app.get("/api/maintenance-evaluation-scores")
async def get_maintenance_evaluation_scores(db: Session = Depends(get_db)):
    scores = get_evaluation_scores(db)
    return JSONResponse(scores)


@app.get("/api/seasonal-comparison")
async def get_seasonal_comparison_api(db: Session = Depends(get_db)):
    comparisons = db.query(models.SeasonalComparison).order_by(
        models.SeasonalComparison.year.asc(),
        models.SeasonalComparison.season
    ).all()
    
    seasons = [f"{c.year}年{c.season}" for c in comparisons]
    total_costs = [round(c.total_maintenance_cost, 2) for c in comparisons]
    total_labors = [round(c.total_labor_hours, 1) for c in comparisons]
    total_yields = [round(c.total_yield, 2) for c in comparisons]
    avg_unit_costs = [c.avg_unit_cost for c in comparisons]
    avg_abnormal_rates = [c.avg_abnormal_rate for c in comparisons]
    avg_overall_scores = [c.avg_overall_score for c in comparisons]
    
    details = []
    for c in comparisons:
        cost_by_type = json.loads(c.cost_by_type) if c.cost_by_type else {}
        labor_by_type = json.loads(c.labor_by_type) if c.labor_by_type else {}
        details.append({
            "year": c.year,
            "season": c.season,
            "total_maintenance_cost": round(c.total_maintenance_cost, 2),
            "total_labor_hours": round(c.total_labor_hours, 1),
            "total_yield": round(c.total_yield, 2),
            "avg_unit_cost": c.avg_unit_cost,
            "avg_abnormal_rate": c.avg_abnormal_rate,
            "avg_overall_score": c.avg_overall_score,
            "tree_count": c.tree_count,
            "incision_count": c.incision_count,
            "cost_by_type": cost_by_type,
            "labor_by_type": labor_by_type
        })
    
    return JSONResponse({
        "seasons": seasons,
        "total_costs": total_costs,
        "total_labors": total_labors,
        "total_yields": total_yields,
        "avg_unit_costs": avg_unit_costs,
        "avg_abnormal_rates": avg_abnormal_rates,
        "avg_overall_scores": avg_overall_scores,
        "details": details
    })


@app.get("/api/seasonal-recommendation")
async def get_seasonal_recommendation_api(db: Session = Depends(get_db)):
    suggestions = get_seasonal_suggestions(db)
    result = {}
    
    for key in ["current", "next"]:
        rec = suggestions[key].get("recommendation")
        if rec:
            result[key] = {
                "season": suggestions[key]["season"],
                "year": suggestions[key]["year"],
                "fertilization_suggestion": rec.fertilization_suggestion,
                "pest_control_suggestion": rec.pest_control_suggestion,
                "bark_care_suggestion": rec.bark_care_suggestion,
                "labor_arrangement_suggestion": rec.labor_arrangement_suggestion,
                "overall_strategy": rec.overall_strategy,
                "key_points": rec.key_points,
                "expected_effect": rec.expected_effect,
                "estimated_cost": rec.estimated_cost,
                "estimated_labor": rec.estimated_labor,
                "generated_at": rec.generated_at.strftime("%Y-%m-%d %H:%M:%S") if rec.generated_at else None
            }
        else:
            result[key] = {
                "season": suggestions[key]["season"],
                "year": suggestions[key]["year"],
                "recommendation": None
            }
    
    return JSONResponse(result)


@app.get("/api/inefficient-maintenance")
async def get_inefficient_maintenance_api(db: Session = Depends(get_db)):
    warnings = get_inefficient_warnings(db)
    return JSONResponse({"inefficient_records": warnings})


@app.get("/api/maintenance-evaluation-detail")
async def get_maintenance_evaluation_detail(
    tree_id: Optional[int] = None,
    incision_id: Optional[int] = None,
    year: Optional[int] = None,
    season: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(models.MaintenanceEvaluation)
    
    if tree_id:
        query = query.filter(models.MaintenanceEvaluation.tree_id == tree_id)
    if incision_id:
        query = query.filter(models.MaintenanceEvaluation.incision_id == incision_id)
    if year:
        query = query.filter(models.MaintenanceEvaluation.year == year)
    if season:
        query = query.filter(models.MaintenanceEvaluation.season == season)
    
    evaluations = query.order_by(
        models.MaintenanceEvaluation.year.desc(),
        models.MaintenanceEvaluation.season,
        models.MaintenanceEvaluation.overall_score
    ).all()
    
    result = []
    for eval in evaluations:
        tree = db.query(models.LacquerTree).filter(models.LacquerTree.id == eval.tree_id).first()
        incision = db.query(models.Incision).filter(models.Incision.id == eval.incision_id).first() if eval.incision_id else None
        result.append({
            "id": eval.id,
            "tree_code": tree.tree_code if tree else "未知",
            "incision_code": incision.incision_code if incision else "整树养护",
            "year": eval.year,
            "season": eval.season,
            "batch_no": eval.batch_no,
            "maintenance_type": eval.maintenance_type,
            "total_maintenance_cost": round(eval.total_maintenance_cost, 2),
            "total_labor_hours": round(eval.total_labor_hours, 1),
            "total_yield": round(eval.total_yield, 2),
            "harvest_count": eval.harvest_count,
            "abnormal_count": eval.abnormal_count,
            "total_observations": eval.total_observations,
            "abnormal_rate": eval.abnormal_rate,
            "avg_recovery_quality": eval.avg_recovery_quality,
            "unit_output_cost": eval.unit_output_cost,
            "input_output_ratio": eval.input_output_ratio,
            "yield_score": eval.yield_score,
            "cost_score": eval.cost_score,
            "quality_score": eval.quality_score,
            "abnormal_score": eval.abnormal_score,
            "overall_score": eval.overall_score,
            "efficiency_level": eval.efficiency_level,
            "is_inefficient": eval.is_inefficient,
            "inefficient_reason": eval.inefficient_reason,
            "suggestions": eval.suggestions,
            "evaluated_at": eval.evaluated_at.strftime("%Y-%m-%d %H:%M:%S") if eval.evaluated_at else None
        })
    
    return JSONResponse({"evaluations": result})


@app.post("/api/recalculate-evaluation")
async def recalculate_evaluation_api(db: Session = Depends(get_db)):
    try:
        recalculate_maintenance_evaluation(db)
        generate_seasonal_recommendations(db)
        recalculate_seasonal_comparisons(db)
        recalculate_quality_analysis(db)
        return JSONResponse({"status": "success", "message": "养护评估和推荐策略已重新计算完成"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/inventory", response_class=HTMLResponse)
async def list_inventory(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = None
):
    query = db.query(models.LacquerInventory)
    if status:
        query = query.filter(models.LacquerInventory.status == status)
    inventories = query.order_by(models.LacquerInventory.storage_date.desc()).all()

    total_stock = sum(inv.stock_quantity for inv in inventories)
    total_value = 0.0
    for inv in inventories:
        sold_qty = sum(s.sale_quantity for s in inv.sales)
        avg_price = sum(s.total_amount for s in inv.sales) / sold_qty if sold_qty > 0 else 0
        total_value += inv.stock_quantity * avg_price

    status_counts = {}
    all_inv = db.query(models.LacquerInventory).all()
    for inv in all_inv:
        s = inv.status or "未知"
        status_counts[s] = status_counts.get(s, 0) + 1

    return templates.TemplateResponse("inventory/list.html", {
        "request": request,
        "inventories": inventories,
        "stats": {
            "total_count": len(inventories),
            "total_stock": round(total_stock, 2),
            "total_value": round(total_value, 2),
            "status_counts": status_counts
        },
        "filter_status": status
    })


@app.get("/inventory/new", response_class=HTMLResponse)
async def new_inventory_form(
    request: Request,
    harvest_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    used_harvest_ids = [inv.harvest_id for inv in db.query(models.LacquerInventory).all()]
    harvests = db.query(models.HarvestBatch).filter(
        ~models.HarvestBatch.id.in_(used_harvest_ids)
    ).order_by(models.HarvestBatch.harvest_date.desc()).all()

    preselected = None
    if harvest_id:
        preselected = db.query(models.HarvestBatch).filter(models.HarvestBatch.id == harvest_id).first()

    today = date.today().strftime("%Y-%m-%d")
    statuses = ["在库", "部分出库", "已售罄", "已过期"]

    return templates.TemplateResponse("inventory/form.html", {
        "request": request,
        "inventory": None,
        "harvests": harvests,
        "statuses": statuses,
        "errors": {},
        "today": today,
        "preselected": preselected
    })


@app.post("/inventory/new")
async def create_inventory(
    request: Request,
    db: Session = Depends(get_db),
    harvest_id: int = Form(...),
    batch_no: str = Form(...),
    storage_location: Optional[str] = Form(None),
    storage_date: str = Form(...),
    stock_quantity: Optional[float] = Form(0.0),
    person_in_charge: Optional[str] = Form(None),
    status: Optional[str] = Form("在库"),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    try:
        s_date = datetime.strptime(storage_date, "%Y-%m-%d").date()
    except ValueError:
        errors["storage_date"] = "日期格式不正确"

    existing = db.query(models.LacquerInventory).filter(models.LacquerInventory.batch_no == batch_no).first()
    if existing:
        errors["batch_no"] = "批次编号已存在"

    existing_harvest = db.query(models.LacquerInventory).filter(models.LacquerInventory.harvest_id == harvest_id).first()
    if existing_harvest:
        errors["harvest_id"] = "该采收批次已入库"

    if stock_quantity is not None and stock_quantity < 0:
        errors["stock_quantity"] = "库存数量不能为负数"

    if errors:
        used_harvest_ids = [inv.harvest_id for inv in db.query(models.LacquerInventory).all()]
        harvests = db.query(models.HarvestBatch).filter(
            ~models.HarvestBatch.id.in_(used_harvest_ids)
        ).order_by(models.HarvestBatch.harvest_date.desc()).all()
        statuses = ["在库", "部分出库", "已售罄", "已过期"]
        return templates.TemplateResponse("inventory/form.html", {
            "request": request,
            "inventory": None,
            "harvests": harvests,
            "statuses": statuses,
            "errors": errors,
            "today": date.today().strftime("%Y-%m-%d"),
            "form_data": {
                "harvest_id": harvest_id, "batch_no": batch_no,
                "storage_location": storage_location, "storage_date": storage_date,
                "stock_quantity": stock_quantity, "person_in_charge": person_in_charge,
                "status": status, "remarks": remarks
            }
        })

    inventory = models.LacquerInventory(
        harvest_id=harvest_id, batch_no=batch_no, storage_location=storage_location,
        storage_date=s_date, stock_quantity=stock_quantity or 0.0,
        person_in_charge=person_in_charge, status=status, remarks=remarks
    )
    db.add(inventory)
    db.commit()
    return RedirectResponse("/inventory", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/inventory/{inv_id}/edit", response_class=HTMLResponse)
async def edit_inventory_form(request: Request, inv_id: int, db: Session = Depends(get_db)):
    inventory = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == inv_id).first()
    if not inventory:
        raise HTTPException(status_code=404, detail="库存记录不存在")
    harvests = db.query(models.HarvestBatch).order_by(models.HarvestBatch.harvest_date.desc()).all()
    statuses = ["在库", "部分出库", "已售罄", "已过期"]
    return templates.TemplateResponse("inventory/form.html", {
        "request": request, "inventory": inventory, "harvests": harvests,
        "statuses": statuses, "errors": {},
        "today": date.today().strftime("%Y-%m-%d")
    })


@app.post("/inventory/{inv_id}/edit")
async def update_inventory(
    request: Request,
    inv_id: int,
    db: Session = Depends(get_db),
    harvest_id: int = Form(...),
    batch_no: str = Form(...),
    storage_location: Optional[str] = Form(None),
    storage_date: str = Form(...),
    stock_quantity: Optional[float] = Form(0.0),
    person_in_charge: Optional[str] = Form(None),
    status: Optional[str] = Form("在库"),
    remarks: Optional[str] = Form(None)
):
    inventory = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == inv_id).first()
    if not inventory:
        raise HTTPException(status_code=404, detail="库存记录不存在")
    errors = {}
    try:
        s_date = datetime.strptime(storage_date, "%Y-%m-%d").date()
    except ValueError:
        errors["storage_date"] = "日期格式不正确"

    existing = db.query(models.LacquerInventory).filter(
        models.LacquerInventory.batch_no == batch_no, models.LacquerInventory.id != inv_id
    ).first()
    if existing:
        errors["batch_no"] = "批次编号已存在"

    existing_harvest = db.query(models.LacquerInventory).filter(
        models.LacquerInventory.harvest_id == harvest_id, models.LacquerInventory.id != inv_id
    ).first()
    if existing_harvest:
        errors["harvest_id"] = "该采收批次已入库"

    if stock_quantity is not None and stock_quantity < 0:
        errors["stock_quantity"] = "库存数量不能为负数"

    if errors:
        harvests = db.query(models.HarvestBatch).order_by(models.HarvestBatch.harvest_date.desc()).all()
        statuses = ["在库", "部分出库", "已售罄", "已过期"]
        return templates.TemplateResponse("inventory/form.html", {
            "request": request, "inventory": inventory, "harvests": harvests,
            "statuses": statuses, "errors": errors,
            "today": date.today().strftime("%Y-%m-%d")
        })

    inventory.harvest_id = harvest_id
    inventory.batch_no = batch_no
    inventory.storage_location = storage_location
    inventory.storage_date = s_date
    inventory.stock_quantity = stock_quantity or 0.0
    inventory.person_in_charge = person_in_charge
    inventory.status = status
    inventory.remarks = remarks
    db.commit()
    return RedirectResponse("/inventory", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/inventory/{inv_id}/delete")
async def delete_inventory(inv_id: int, db: Session = Depends(get_db)):
    inventory = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == inv_id).first()
    if not inventory:
        raise HTTPException(status_code=404, detail="库存记录不存在")
    db.delete(inventory)
    db.commit()
    return RedirectResponse("/inventory", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/sales", response_class=HTMLResponse)
async def list_sales(
    request: Request,
    db: Session = Depends(get_db),
    payment_status: Optional[str] = None
):
    query = db.query(models.LacquerSale)
    if payment_status:
        query = query.filter(models.LacquerSale.payment_status == payment_status)
    sales = query.order_by(models.LacquerSale.sale_date.desc()).all()

    total_qty = sum(s.sale_quantity for s in sales)
    total_amount = sum(s.total_amount for s in sales)
    unpaid_amount = sum(s.total_amount for s in sales if s.payment_status == "未收款")

    return templates.TemplateResponse("sales/list.html", {
        "request": request,
        "sales": sales,
        "stats": {
            "total_count": len(sales),
            "total_qty": round(total_qty, 2),
            "total_amount": round(total_amount, 2),
            "unpaid_amount": round(unpaid_amount, 2)
        },
        "filter_payment": payment_status
    })


@app.get("/sales/new", response_class=HTMLResponse)
async def new_sale_form(
    request: Request,
    inventory_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    inventories = db.query(models.LacquerInventory).filter(
        models.LacquerInventory.stock_quantity > 0
    ).order_by(models.LacquerInventory.storage_date.desc()).all()
    today = date.today().strftime("%Y-%m-%d")
    payment_statuses = ["未收款", "部分收款", "已收款"]
    grades = ["特级", "一级", "二级", "三级"]

    preselected = None
    if inventory_id:
        preselected = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == inventory_id).first()

    return templates.TemplateResponse("sales/form.html", {
        "request": request,
        "sale": None,
        "inventories": inventories,
        "payment_statuses": payment_statuses,
        "grades": grades,
        "errors": {},
        "today": today,
        "preselected": preselected
    })


@app.post("/sales/new")
async def create_sale(
    request: Request,
    db: Session = Depends(get_db),
    inventory_id: int = Form(...),
    sale_date: str = Form(...),
    customer: Optional[str] = Form(None),
    sale_quantity: float = Form(...),
    unit_price: Optional[float] = Form(0.0),
    total_amount: Optional[float] = Form(0.0),
    destination: Optional[str] = Form(None),
    quality_grade: Optional[str] = Form(None),
    person_in_charge: Optional[str] = Form(None),
    payment_status: Optional[str] = Form("未收款"),
    remarks: Optional[str] = Form(None)
):
    errors = {}
    try:
        s_date = datetime.strptime(sale_date, "%Y-%m-%d").date()
    except ValueError:
        errors["sale_date"] = "日期格式不正确"

    if sale_quantity <= 0:
        errors["sale_quantity"] = "销售数量必须大于0"

    inventory = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == inventory_id).first()
    if inventory and sale_quantity > inventory.stock_quantity:
        errors["sale_quantity"] = f"库存不足，当前库存：{inventory.stock_quantity}kg"

    if unit_price is not None and unit_price < 0:
        errors["unit_price"] = "单价不能为负数"
    if total_amount is not None and total_amount < 0:
        errors["total_amount"] = "总金额不能为负数"

    if errors:
        inventories = db.query(models.LacquerInventory).filter(
            models.LacquerInventory.stock_quantity > 0
        ).order_by(models.LacquerInventory.storage_date.desc()).all()
        payment_statuses = ["未收款", "部分收款", "已收款"]
        grades = ["特级", "一级", "二级", "三级"]
        return templates.TemplateResponse("sales/form.html", {
            "request": request,
            "sale": None,
            "inventories": inventories,
            "payment_statuses": payment_statuses,
            "grades": grades,
            "errors": errors,
            "today": date.today().strftime("%Y-%m-%d"),
            "form_data": {
                "inventory_id": inventory_id, "sale_date": sale_date,
                "customer": customer, "sale_quantity": sale_quantity,
                "unit_price": unit_price, "total_amount": total_amount,
                "destination": destination, "quality_grade": quality_grade,
                "person_in_charge": person_in_charge,
                "payment_status": payment_status, "remarks": remarks
            }
        })

    calc_total = round((unit_price or 0.0) * sale_quantity, 2) if not total_amount else (total_amount or 0.0)

    sale = models.LacquerSale(
        inventory_id=inventory_id, sale_date=s_date, customer=customer,
        sale_quantity=sale_quantity, unit_price=unit_price or 0.0,
        total_amount=calc_total, destination=destination,
        quality_grade=quality_grade, person_in_charge=person_in_charge,
        payment_status=payment_status, remarks=remarks
    )
    db.add(sale)

    if inventory:
        inventory.stock_quantity = round(inventory.stock_quantity - sale_quantity, 3)
        if inventory.stock_quantity <= 0:
            inventory.status = "已售罄"
            inventory.stock_quantity = 0.0
        elif inventory.stock_quantity < (db.query(func.sum(models.HarvestBatch.yield_amount)).filter(
            models.HarvestBatch.id == inventory.harvest_id
        ).scalar() or 0):
            inventory.status = "部分出库"

    db.commit()
    return RedirectResponse("/sales", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/sales/{sale_id}/edit", response_class=HTMLResponse)
async def edit_sale_form(request: Request, sale_id: int, db: Session = Depends(get_db)):
    sale = db.query(models.LacquerSale).filter(models.LacquerSale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="销售记录不存在")
    inventories = db.query(models.LacquerInventory).order_by(models.LacquerInventory.storage_date.desc()).all()
    payment_statuses = ["未收款", "部分收款", "已收款"]
    grades = ["特级", "一级", "二级", "三级"]
    return templates.TemplateResponse("sales/form.html", {
        "request": request, "sale": sale, "inventories": inventories,
        "payment_statuses": payment_statuses, "grades": grades,
        "errors": {}, "today": date.today().strftime("%Y-%m-%d")
    })


@app.post("/sales/{sale_id}/edit")
async def update_sale(
    request: Request,
    sale_id: int,
    db: Session = Depends(get_db),
    inventory_id: int = Form(...),
    sale_date: str = Form(...),
    customer: Optional[str] = Form(None),
    sale_quantity: float = Form(...),
    unit_price: Optional[float] = Form(0.0),
    total_amount: Optional[float] = Form(0.0),
    destination: Optional[str] = Form(None),
    quality_grade: Optional[str] = Form(None),
    person_in_charge: Optional[str] = Form(None),
    payment_status: Optional[str] = Form("未收款"),
    remarks: Optional[str] = Form(None)
):
    sale = db.query(models.LacquerSale).filter(models.LacquerSale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="销售记录不存在")
    errors = {}
    try:
        s_date = datetime.strptime(sale_date, "%Y-%m-%d").date()
    except ValueError:
        errors["sale_date"] = "日期格式不正确"

    if sale_quantity <= 0:
        errors["sale_quantity"] = "销售数量必须大于0"

    old_inventory = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == sale.inventory_id).first()
    new_inventory = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == inventory_id).first()

    if old_inventory and old_inventory.id == inventory_id:
        available = old_inventory.stock_quantity + sale.sale_quantity
        if sale_quantity > available:
            errors["sale_quantity"] = f"库存不足，可用：{available}kg"
    elif new_inventory and sale_quantity > new_inventory.stock_quantity:
        errors["sale_quantity"] = f"库存不足，当前库存：{new_inventory.stock_quantity}kg"

    if unit_price is not None and unit_price < 0:
        errors["unit_price"] = "单价不能为负数"
    if total_amount is not None and total_amount < 0:
        errors["total_amount"] = "总金额不能为负数"

    if errors:
        inventories = db.query(models.LacquerInventory).order_by(models.LacquerInventory.storage_date.desc()).all()
        payment_statuses = ["未收款", "部分收款", "已收款"]
        grades = ["特级", "一级", "二级", "三级"]
        return templates.TemplateResponse("sales/form.html", {
            "request": request, "sale": sale, "inventories": inventories,
            "payment_statuses": payment_statuses, "grades": grades,
            "errors": {}, "today": date.today().strftime("%Y-%m-%d")
        })

    if old_inventory and old_inventory.id != inventory_id:
        old_inventory.stock_quantity = round(old_inventory.stock_quantity + sale.sale_quantity, 3)
        original_yield = db.query(func.sum(models.HarvestBatch.yield_amount)).filter(
            models.HarvestBatch.id == old_inventory.harvest_id
        ).scalar() or 0
        if old_inventory.stock_quantity >= original_yield:
            old_inventory.status = "在库"
        elif old_inventory.stock_quantity > 0:
            old_inventory.status = "部分出库"
        else:
            old_inventory.status = "已售罄"

    if new_inventory and new_inventory.id == inventory_id:
        if old_inventory and old_inventory.id == inventory_id:
            new_inventory.stock_quantity = round(new_inventory.stock_quantity + sale.sale_quantity - sale_quantity, 3)
        else:
            new_inventory.stock_quantity = round(new_inventory.stock_quantity - sale_quantity, 3)
        original_yield = db.query(func.sum(models.HarvestBatch.yield_amount)).filter(
            models.HarvestBatch.id == new_inventory.harvest_id
        ).scalar() or 0
        if new_inventory.stock_quantity <= 0:
            new_inventory.status = "已售罄"
            new_inventory.stock_quantity = 0.0
        elif new_inventory.stock_quantity >= original_yield:
            new_inventory.status = "在库"
        else:
            new_inventory.status = "部分出库"

    calc_total = round((unit_price or 0.0) * sale_quantity, 2) if not total_amount else (total_amount or 0.0)

    sale.inventory_id = inventory_id
    sale.sale_date = s_date
    sale.customer = customer
    sale.sale_quantity = sale_quantity
    sale.unit_price = unit_price or 0.0
    sale.total_amount = calc_total
    sale.destination = destination
    sale.quality_grade = quality_grade
    sale.person_in_charge = person_in_charge
    sale.payment_status = payment_status
    sale.remarks = remarks
    db.commit()
    return RedirectResponse("/sales", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/sales/{sale_id}/delete")
async def delete_sale(sale_id: int, db: Session = Depends(get_db)):
    sale = db.query(models.LacquerSale).filter(models.LacquerSale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="销售记录不存在")

    inventory = db.query(models.LacquerInventory).filter(models.LacquerInventory.id == sale.inventory_id).first()
    if inventory:
        inventory.stock_quantity = round(inventory.stock_quantity + sale.sale_quantity, 3)
        original_yield = db.query(func.sum(models.HarvestBatch.yield_amount)).filter(
            models.HarvestBatch.id == inventory.harvest_id
        ).scalar() or 0
        if inventory.stock_quantity >= original_yield:
            inventory.status = "在库"
        elif inventory.stock_quantity > 0:
            inventory.status = "部分出库"
        else:
            inventory.status = "已售罄"

    db.delete(sale)
    db.commit()
    return RedirectResponse("/sales", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/quality-analysis", response_class=HTMLResponse)
async def quality_analysis_page(request: Request, db: Session = Depends(get_db)):
    recalculate_quality_analysis(db)

    tree_analyses = db.query(models.QualityAnalysis).filter(
        models.QualityAnalysis.analysis_type == "tree"
    ).order_by(models.QualityAnalysis.high_grade_rate.desc()).all()

    incision_analyses = db.query(models.QualityAnalysis).filter(
        models.QualityAnalysis.analysis_type == "incision"
    ).order_by(models.QualityAnalysis.high_grade_rate.desc()).all()

    weather_analyses = db.query(models.QualityAnalysis).filter(
        models.QualityAnalysis.analysis_type == "weather"
    ).order_by(models.QualityAnalysis.high_grade_rate.desc()).all()

    maintenance_analyses = db.query(models.QualityAnalysis).filter(
        models.QualityAnalysis.analysis_type == "maintenance"
    ).order_by(models.QualityAnalysis.high_grade_rate.desc()).all()

    quality_warnings = get_quality_warnings(db)
    inventory_warnings = get_inventory_turnover_warnings(db)
    high_grade_patterns = get_high_grade_patterns(db)

    all_harvests = db.query(models.HarvestBatch).all()
    grade_distribution = {}
    for h in all_harvests:
        if h.quality_grade:
            grade_distribution[h.quality_grade] = grade_distribution.get(h.quality_grade, 0) + 1

    return templates.TemplateResponse("quality_analysis.html", {
        "request": request,
        "tree_analyses": tree_analyses,
        "incision_analyses": incision_analyses,
        "weather_analyses": weather_analyses,
        "maintenance_analyses": maintenance_analyses,
        "quality_warnings": quality_warnings,
        "inventory_warnings": inventory_warnings,
        "high_grade_patterns": high_grade_patterns,
        "grade_distribution": grade_distribution,
        "total_harvests": len(all_harvests)
    })


@app.get("/api/quality-analysis")
async def get_quality_analysis_api(
    analysis_type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(models.QualityAnalysis)
    if analysis_type:
        query = query.filter(models.QualityAnalysis.analysis_type == analysis_type)
    analyses = query.order_by(models.QualityAnalysis.high_grade_rate.desc()).all()
    result = []
    for a in analyses:
        grade_counts = json.loads(a.grade_counts) if a.grade_counts else {}
        result.append({
            "id": a.id,
            "analysis_type": a.analysis_type,
            "analysis_key": a.analysis_key,
            "total_count": a.total_count,
            "avg_yield": a.avg_yield,
            "grade_counts": grade_counts,
            "high_grade_rate": a.high_grade_rate,
            "avg_impurity": a.avg_impurity,
            "avg_moisture": a.avg_moisture,
            "avg_viscosity": a.avg_viscosity
        })
    return JSONResponse({"analyses": result})


@app.get("/api/quality-warnings")
async def get_quality_warnings_api(db: Session = Depends(get_db)):
    warnings = get_quality_warnings(db)
    return JSONResponse({"warnings": warnings})


@app.get("/api/inventory-turnover-warnings")
async def get_inventory_turnover_warnings_api(db: Session = Depends(get_db)):
    warnings = get_inventory_turnover_warnings(db)
    return JSONResponse({"warnings": warnings})


@app.get("/api/high-grade-patterns")
async def get_high_grade_patterns_api(db: Session = Depends(get_db)):
    patterns = get_high_grade_patterns(db)
    return JSONResponse(patterns)
