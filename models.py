from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, Text, DateTime, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class LacquerTree(Base):
    __tablename__ = "lacquer_trees"

    id = Column(Integer, primary_key=True, index=True)
    tree_code = Column(String(50), unique=True, nullable=False, index=True)
    species = Column(String(100))
    age = Column(Integer)
    location = Column(String(200))
    altitude = Column(Float)
    soil_type = Column(String(100))
    planting_date = Column(Date)
    status = Column(String(20), default="正常")
    remarks = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    incisions = relationship("Incision", back_populates="tree", cascade="all, delete-orphan")
    observations = relationship("RecoveryObservation", back_populates="tree", cascade="all, delete-orphan")


class Incision(Base):
    __tablename__ = "incisions"

    id = Column(Integer, primary_key=True, index=True)
    tree_id = Column(Integer, ForeignKey("lacquer_trees.id"), nullable=False)
    incision_code = Column(String(50), nullable=False)
    position = Column(String(100), nullable=False)
    height = Column(Float)
    method = Column(String(50), nullable=False)
    incision_date = Column(Date, nullable=False)
    recovery_days = Column(Integer, default=7)
    total_harvests = Column(Integer, default=0)
    total_yield = Column(Float, default=0.0)
    avg_yield = Column(Float, default=0.0)
    status = Column(String(20), default="活跃")
    remarks = Column(Text)

    tree = relationship("LacquerTree", back_populates="incisions")
    harvests = relationship("HarvestBatch", back_populates="incision", cascade="all, delete-orphan")


class HarvestBatch(Base):
    __tablename__ = "harvest_batches"

    id = Column(Integer, primary_key=True, index=True)
    incision_id = Column(Integer, ForeignKey("incisions.id"), nullable=False)
    harvest_date = Column(Date, nullable=False)
    yield_amount = Column(Float, nullable=False)
    quality_grade = Column(String(20))
    weather_id = Column(Integer, ForeignKey("weather_conditions.id"))
    operator = Column(String(50))
    remarks = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    incision = relationship("Incision", back_populates="harvests")
    weather = relationship("WeatherCondition")


class WeatherCondition(Base):
    __tablename__ = "weather_conditions"

    id = Column(Integer, primary_key=True, index=True)
    record_date = Column(Date, nullable=False)
    temperature = Column(Float)
    humidity = Column(Float)
    weather_type = Column(String(50))
    wind_speed = Column(Float)
    remarks = Column(Text)

    harvests = relationship("HarvestBatch", back_populates="weather")


class RecoveryObservation(Base):
    __tablename__ = "recovery_observations"

    id = Column(Integer, primary_key=True, index=True)
    tree_id = Column(Integer, ForeignKey("lacquer_trees.id"), nullable=False)
    observation_date = Column(Date, nullable=False)
    incision_id = Column(Integer, ForeignKey("incisions.id"))
    tree_condition = Column(String(20), nullable=False)
    bark_healing = Column(String(100))
    sap_flow = Column(String(100))
    leaf_condition = Column(String(100))
    is_abnormal = Column(Boolean, default=False)
    treatment_suggestion = Column(Text)
    observer = Column(String(50))
    remarks = Column(Text)

    tree = relationship("LacquerTree", back_populates="observations")
    incision = relationship("Incision")


class HarvestPlan(Base):
    __tablename__ = "harvest_plans"

    id = Column(Integer, primary_key=True, index=True)
    tree_id = Column(Integer, ForeignKey("lacquer_trees.id"), nullable=False)
    incision_id = Column(Integer, ForeignKey("incisions.id"), nullable=False)
    plan_date = Column(Date, nullable=False)
    harvest_method = Column(String(50), nullable=False)
    person_in_charge = Column(String(50))
    status = Column(String(20), default="待执行")
    actual_harvest_id = Column(Integer, ForeignKey("harvest_batches.id"))
    remarks = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tree = relationship("LacquerTree")
    incision = relationship("Incision")
    actual_harvest = relationship("HarvestBatch")


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"

    id = Column(Integer, primary_key=True, index=True)
    tree_id = Column(Integer, ForeignKey("lacquer_trees.id"), nullable=False)
    incision_id = Column(Integer, ForeignKey("incisions.id"))
    maintenance_date = Column(Date, nullable=False)
    project_type = Column(String(50), nullable=False)
    batch_no = Column(String(50))
    quantity = Column(Float, default=0.0)
    unit = Column(String(20))
    unit_price = Column(Float, default=0.0)
    total_cost = Column(Float, default=0.0)
    labor_hours = Column(Float, default=0.0)
    labor_cost_rate = Column(Float, default=0.0)
    labor_cost = Column(Float, default=0.0)
    person_in_charge = Column(String(50))
    remarks = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tree = relationship("LacquerTree")
    incision = relationship("Incision")


class MaintenanceEvaluation(Base):
    __tablename__ = "maintenance_evaluations"

    id = Column(Integer, primary_key=True, index=True)
    tree_id = Column(Integer, ForeignKey("lacquer_trees.id"), nullable=False)
    incision_id = Column(Integer, ForeignKey("incisions.id"))
    season = Column(String(20), nullable=False)
    year = Column(Integer, nullable=False)
    batch_no = Column(String(50))
    maintenance_type = Column(String(50))
    
    total_maintenance_cost = Column(Float, default=0.0)
    total_labor_hours = Column(Float, default=0.0)
    total_yield = Column(Float, default=0.0)
    harvest_count = Column(Integer, default=0)
    abnormal_count = Column(Integer, default=0)
    total_observations = Column(Integer, default=0)
    abnormal_rate = Column(Float, default=0.0)
    
    avg_recovery_quality = Column(Float, default=0.0)
    unit_output_cost = Column(Float, default=0.0)
    input_output_ratio = Column(Float, default=0.0)
    
    yield_score = Column(Float, default=0.0)
    cost_score = Column(Float, default=0.0)
    quality_score = Column(Float, default=0.0)
    abnormal_score = Column(Float, default=0.0)
    overall_score = Column(Float, default=0.0)
    efficiency_level = Column(String(20), default="中等")
    
    is_inefficient = Column(Boolean, default=False)
    inefficient_reason = Column(Text)
    suggestions = Column(Text)
    
    evaluated_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    tree = relationship("LacquerTree")
    incision = relationship("Incision")


class SeasonalRecommendation(Base):
    __tablename__ = "seasonal_recommendations"

    id = Column(Integer, primary_key=True, index=True)
    season = Column(String(20), nullable=False)
    year = Column(Integer, nullable=False)
    
    fertilization_suggestion = Column(Text)
    pest_control_suggestion = Column(Text)
    bark_care_suggestion = Column(Text)
    labor_arrangement_suggestion = Column(Text)
    
    overall_strategy = Column(Text)
    key_points = Column(Text)
    
    expected_effect = Column(Text)
    estimated_cost = Column(Float, default=0.0)
    estimated_labor = Column(Float, default=0.0)
    
    generated_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class SeasonalComparison(Base):
    __tablename__ = "seasonal_comparisons"

    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, nullable=False)
    season = Column(String(20), nullable=False)
    
    total_maintenance_cost = Column(Float, default=0.0)
    total_labor_hours = Column(Float, default=0.0)
    total_yield = Column(Float, default=0.0)
    avg_unit_cost = Column(Float, default=0.0)
    avg_abnormal_rate = Column(Float, default=0.0)
    avg_overall_score = Column(Float, default=0.0)
    tree_count = Column(Integer, default=0)
    incision_count = Column(Integer, default=0)
    
    cost_by_type = Column(Text)
    labor_by_type = Column(Text)
    
    generated_at = Column(DateTime(timezone=True), server_default=func.now())
