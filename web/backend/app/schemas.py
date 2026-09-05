from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=4, max_length=100)
    full_name: str = Field(min_length=1, max_length=150)
    role: str = "seller"


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class LeadOut(BaseModel):
    id: int
    name: str
    categories: list
    address: str
    phone: str
    email: str
    website: str
    website_status: str
    website_platform: str
    social_links: list
    rating: str
    reviews: str
    hours: str
    yandex_url: str
    source: str
    scraped_at: Optional[datetime] = None
    status: str
    assigned_to: Optional[int] = None
    assigned_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    notes: str
    assigned_seller_name: Optional[str] = None

    model_config = {"from_attributes": True}


class LeadStatusUpdate(BaseModel):
    status: str
    note: str = ""


class LeadAssign(BaseModel):
    seller_id: int


class LeadBatchImport(BaseModel):
    leads: list[dict]


class CallLogOut(BaseModel):
    id: int
    lead_id: int
    seller_id: int
    action: str
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    note: str
    created_at: datetime
    seller_name: Optional[str] = None

    model_config = {"from_attributes": True}


class DashboardStats(BaseModel):
    total_leads: int
    new_leads: int
    assigned_leads: int
    confirmed_leads: int
    declined_leads: int
    followup_leads: int
    no_answer_leads: int
    calling_leads: int
    waiting_leads: int
    total_sellers: int
    active_sellers: int
    conversion_rate: float
    seller_stats: list[dict] = []


class RateLimitInfo(BaseModel):
    actions_remaining: int
    actions_used: int
    limit: int
    resets_at: datetime


class PortfolioCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str = ""
    sort_order: int = 0
    is_active: bool = True


class PortfolioCategoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class PortfolioCategoryOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str
    sort_order: int
    is_active: bool
    created_at: datetime
    projects_count: int = 0

    model_config = {"from_attributes": True}


class PortfolioProjectCreate(BaseModel):
    category_id: int
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    client_name: str = ""


class PortfolioProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    client_name: Optional[str] = None


class PortfolioScreenshotOut(BaseModel):
    id: int
    project_id: int
    filename: str
    original_filename: str
    url: str
    sort_order: int
    created_at: datetime

    model_config = {"from_attributes": True}


class PortfolioProjectOut(BaseModel):
    id: int
    category_id: int
    name: str
    slug: str
    description: str
    client_name: str
    created_at: datetime
    screenshots: list[PortfolioScreenshotOut] = []

    model_config = {"from_attributes": True}
