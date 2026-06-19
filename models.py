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
    quantity = Column(Float, default=0.0)
    unit = Column(String(20))
    unit_price = Column(Float, default=0.0)
    total_cost = Column(Float, default=0.0)
    labor_hours = Column(Float, default=0.0)
    person_in_charge = Column(String(50))
    remarks = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tree = relationship("LacquerTree")
    incision = relationship("Incision")
