from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel, field_validator


class TreeBase(BaseModel):
    tree_code: str
    species: Optional[str] = None
    age: Optional[int] = None
    location: Optional[str] = None
    altitude: Optional[float] = None
    soil_type: Optional[str] = None
    planting_date: Optional[date] = None
    status: Optional[str] = "正常"
    remarks: Optional[str] = None


class TreeCreate(TreeBase):
    pass


class TreeUpdate(TreeBase):
    pass


class Tree(TreeBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class IncisionBase(BaseModel):
    tree_id: int
    incision_code: str
    position: str
    height: Optional[float] = None
    method: str
    incision_date: date
    recovery_days: Optional[int] = 7
    status: Optional[str] = "活跃"
    remarks: Optional[str] = None


class IncisionCreate(IncisionBase):
    pass


class IncisionUpdate(IncisionBase):
    pass


class Incision(IncisionBase):
    id: int
    total_harvests: int = 0
    total_yield: float = 0.0
    avg_yield: float = 0.0

    class Config:
        from_attributes = True


class HarvestBase(BaseModel):
    incision_id: int
    harvest_date: date
    yield_amount: float
    color: Optional[str] = None
    impurity: Optional[float] = None
    moisture: Optional[float] = None
    viscosity: Optional[float] = None
    quality_grade: Optional[str] = None
    weather_id: Optional[int] = None
    operator: Optional[str] = None
    remarks: Optional[str] = None

    @field_validator("yield_amount")
    @classmethod
    def yield_must_be_non_negative(cls, v):
        if v < 0:
            raise ValueError("出漆量不能为负数")
        return v

    @field_validator("impurity", "moisture", "viscosity")
    @classmethod
    def quality_params_non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("参数不能为负数")
        return v


class HarvestCreate(HarvestBase):
    pass


class HarvestUpdate(HarvestBase):
    pass


class Harvest(HarvestBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InventoryBase(BaseModel):
    harvest_id: int
    batch_no: str
    storage_location: Optional[str] = None
    storage_date: date
    stock_quantity: Optional[float] = 0.0
    person_in_charge: Optional[str] = None
    status: Optional[str] = "在库"
    remarks: Optional[str] = None

    @field_validator("stock_quantity")
    @classmethod
    def quantity_non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("库存数量不能为负数")
        return v


class InventoryCreate(InventoryBase):
    pass


class InventoryUpdate(InventoryBase):
    pass


class Inventory(InventoryBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SaleBase(BaseModel):
    inventory_id: int
    sale_date: date
    customer: Optional[str] = None
    sale_quantity: float
    unit_price: Optional[float] = 0.0
    total_amount: Optional[float] = 0.0
    destination: Optional[str] = None
    quality_grade: Optional[str] = None
    person_in_charge: Optional[str] = None
    payment_status: Optional[str] = "未收款"
    remarks: Optional[str] = None

    @field_validator("sale_quantity", "unit_price", "total_amount")
    @classmethod
    def values_non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("参数不能为负数")
        return v


class SaleCreate(SaleBase):
    pass


class SaleUpdate(SaleBase):
    pass


class Sale(SaleBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WeatherBase(BaseModel):
    record_date: date
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    weather_type: Optional[str] = None
    wind_speed: Optional[float] = None
    remarks: Optional[str] = None


class WeatherCreate(WeatherBase):
    pass


class WeatherUpdate(WeatherBase):
    pass


class Weather(WeatherBase):
    id: int

    class Config:
        from_attributes = True


class ObservationBase(BaseModel):
    tree_id: int
    observation_date: date
    incision_id: Optional[int] = None
    tree_condition: str
    bark_healing: Optional[str] = None
    sap_flow: Optional[str] = None
    leaf_condition: Optional[str] = None
    is_abnormal: Optional[bool] = False
    treatment_suggestion: Optional[str] = None
    observer: Optional[str] = None
    remarks: Optional[str] = None


class ObservationCreate(ObservationBase):
    @field_validator("treatment_suggestion")
    @classmethod
    def check_treatment_when_abnormal(cls, v, info):
        if info.data.get("is_abnormal") and not v:
            raise ValueError("树体状态异常时必须填写处理建议")
        return v


class ObservationUpdate(ObservationBase):
    @field_validator("treatment_suggestion")
    @classmethod
    def check_treatment_when_abnormal(cls, v, info):
        if info.data.get("is_abnormal") and not v:
            raise ValueError("树体状态异常时必须填写处理建议")
        return v


class Observation(ObservationBase):
    id: int

    class Config:
        from_attributes = True


class HarvestPlanBase(BaseModel):
    tree_id: int
    incision_id: int
    plan_date: date
    harvest_method: str
    person_in_charge: Optional[str] = None
    status: Optional[str] = "待执行"
    remarks: Optional[str] = None


class HarvestPlanCreate(HarvestPlanBase):
    @field_validator("plan_date")
    @classmethod
    def plan_date_not_past(cls, v):
        if v < date.today():
            raise ValueError("计划日期不能早于当前日期")
        return v


class HarvestPlanUpdate(HarvestPlanBase):
    actual_harvest_id: Optional[int] = None


class HarvestPlan(HarvestPlanBase):
    id: int
    actual_harvest_id: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MaintenanceBase(BaseModel):
    tree_id: int
    incision_id: Optional[int] = None
    maintenance_date: date
    project_type: str
    batch_no: Optional[str] = None
    quantity: Optional[float] = 0.0
    unit: Optional[str] = None
    unit_price: Optional[float] = 0.0
    total_cost: Optional[float] = 0.0
    labor_hours: Optional[float] = 0.0
    labor_cost_rate: Optional[float] = 0.0
    labor_cost: Optional[float] = 0.0
    person_in_charge: Optional[str] = None
    remarks: Optional[str] = None


class MaintenanceCreate(MaintenanceBase):
    @field_validator("quantity", "unit_price", "total_cost", "labor_hours", "labor_cost_rate", "labor_cost")
    @classmethod
    def non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("不能为负数")
        return v


class MaintenanceUpdate(MaintenanceBase):
    @field_validator("quantity", "unit_price", "total_cost", "labor_hours", "labor_cost_rate", "labor_cost")
    @classmethod
    def non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("不能为负数")
        return v


class Maintenance(MaintenanceBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
