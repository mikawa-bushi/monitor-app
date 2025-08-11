# Monitor App 🚀

Monitor App is a high-performance data monitoring and management application that automatically generates SQLite databases from CSV data and manages data through both Web UI and REST API.

**🎯 Main Use Cases:**
- **Rapid CSV Data Visualization**: Instantly create web apps and APIs by simply placing CSV files
- **Quick Frontend for Existing DB Systems**: Add Web UI and REST API to existing MySQL/PostgreSQL systems with just configuration changes
- **Accelerated Prototype Development**: Complete full-featured CRUD applications by just defining data structures

**🔧 Key Features:**
- **Comprehensive REST API**: Full CRUD operations (Create, Read, Update, Delete) support
- **Automatic API Documentation**: FastAPI-like automatic documentation generation with Swagger UI (`/docs`)
- **Separated Architecture**: Clear separation between CRUD operation tables and display views
- **Flexible View Display**: Custom query-based data display (JOIN, aggregation, filtering, etc.), real-time updates (2-second intervals)
- **Advanced Styling Features**: Conditional cell styling based on values (color coding, font size, alignment)
- **CORS Support**: Easy integration with frontend applications
- **Comprehensive Testing**: Automated tests for REST API, Web UI, and all configurations

This project is inspired by Flask and Django, specifically built on Flask as the foundation.  
Designed to be accessible for Python beginners and aimed at rapid web app development for manufacturing and data analysis tasks.

## 📌 Features
- Create new projects with `monitor-app startproject`
- Easy conversion of CSV data to SQLite
- Display and edit data through Web UI
- Support for SQLite / MySQL / PostgreSQL using `Flask-SQLAlchemy`
- Stylish UI using Bootstrap
- Customizable through `config.py`
- Automatic generation of full-featured REST API
- Real-time data update functionality

---

## 🎯 User Benefits

### **⚡ Outstanding Development Speed**
- **Instant Web App from CSV**: Complete full-featured web applications by simply placing CSV files
- **Zero-Coding REST API**: Automatically generate CRUD-capable REST APIs without programming
- **Instant Integration with Existing Systems**: Add web interfaces to existing MySQL/PostgreSQL databases with just configuration changes

### **🔧 High Flexibility and Extensibility**
- **Multi-Database Support**: Switch between SQLite (development/prototype) and MySQL/PostgreSQL (production) through configuration
- **Customizable UI**: Visual representation of data importance and status through conditional styling features
- **JOIN Functionality**: Integrated display of related data from multiple tables, intuitive understanding of complex data structures

### **👥 Enhanced Team Collaboration**
- **Non-Engineer Friendly**: Anyone can manipulate data through the Web UI
- **API Integration**: Frontend teams can freely build UIs using REST APIs
- **Real-time Updates**: Always display the latest data even when multiple users work simultaneously

### **🛡️ Safe and Reliable Operation**
- **Comprehensive Test Coverage**: Automated tests for all API, UI, and configuration features
- **Error Handling**: Proper error handling for invalid data and operations
- **CORS Support**: Support for secure cross-origin communication

### **💰 Cost Reduction Benefits**
- **Reduced Development Hours**: Shorten development work from days/weeks to minutes
- **Reduced Maintenance Costs**: Long-term operation assured with Flask-based simple architecture
- **Minimized Learning Costs**: Designed to be accessible for Python beginners

---

## 🚀 Installation Method
Easy installation with `pip install`.

```sh
pip install monitor-app
```

---

## 🔧 Usage

### **1️⃣ Create a New Project**
```sh
monitor-app startproject <project_name>
```
📌 **Example: Create template with the name my_project**
```sh
monitor-app startproject my_project
```
➡ Creates a Monitor-app application template in the `my_project` folder.


### **2️⃣ Register CSV to Database**
```sh
cd my_project
python <project_name>/app.py import-csv
```
➡ Converts CSV files in the `csv/` folder to SQLite database.

### **3️⃣ Start Web Application**
```sh
python <project_name>/app.py runserver
```
➡ Access `http://127.0.0.1:9990`!

### **📌 `runserver` Options**
| Option | Description |
|------------|--------------------------------|
| `--csv`   | Register CSV before starting  |
| `--debug` | Start in debug mode    |
| `--port <PORT>` | Specify port (default: 9990) |

📌 **Example: Start after registering CSV**
```sh
python <project_name>/app.py runserver --csv
```

📌 **Example: Start in debug mode on port `8000`**
```sh
python <project_name>/app.py runserver --debug --port 8000
```

---

## 📂 Folder Structure
```sh
my_project/
│── monitor_app/
│   ├── app.py           # Main Flask application file
│   ├── cli.py           # Command file for template creation
│   ├── csv_to_db.py     # Script to import CSV to database
│   ├── config/
│   │    ├── config.py   # Configuration file
│   ├── templates/       # HTML templates
│   ├── static/          # CSS / JavaScript / Images
│   ├── csv/             # Folder to store CSV data
│   ├── instances/       # SQLite database storage location
│── pyproject.toml       # Poetry configuration file
│── README.md            # This file
```

---

## 🔧 `config/config.py` Configuration

All project settings can be changed in `config/config.py`. The following are details of all configurable items.

### **📌 Database Settings**

#### **Database Type Selection**
```python
# Specify the type of database to use
DB_TYPE = "sqlite"  # "sqlite" | "mysql" | "postgresql"
```

#### **SQLite Settings**
```python
# Custom path settings for SQLite
CUSTOM_SQLITE_DB_PATH = None  # Uses instances/database.db if None
# CUSTOM_SQLITE_DB_PATH = "/path/to/custom/database.db"  # Custom path specification
```

#### **MySQL Settings**
```python
# Used when DB_TYPE = "mysql"
MYSQL_USER = "root"
MYSQL_PASSWORD = "password"
MYSQL_HOST = "localhost"
MYSQL_PORT = "3306"
MYSQL_DB = "monitor_app"
```

#### **PostgreSQL Settings**
```python
# Used when DB_TYPE = "postgresql"
POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "password"
POSTGRES_HOST = "localhost"
POSTGRES_PORT = "5432"
POSTGRES_DB = "monitor_app"
```

### **📌 Separated Configuration Design**

Monitor App clearly separates settings for **CRUD operations** and **display purposes**:

- **`ALLOWED_TABLES`**: Table definitions for CRUD API operations
- **`VIEW_TABLES`**: View definitions for screen display  
- **`TABLE_CELL_STYLES`**: Styling settings for view display

This separation maintains data operation security while achieving display flexibility.

### **📌 CRUD Operation Settings (ALLOWED_TABLES)**

`ALLOWED_TABLES` defines tables that can be CRUD operated via REST API. For security, tables not defined here cannot be accessed at all.

#### **Role of ALLOWED_TABLES**
- **Security**: Prevent unauthorized table access
- **Automatic REST API Generation**: Automatically create CRUD APIs only for defined tables
- **Input Validation**: Column validation for POST/PUT requests
- **Data Structure Definition**: Management of primary key and foreign key information

#### **Basic CRUD Configuration**
```python
ALLOWED_TABLES = {
    "users": {
        "columns": ["id", "name", "email"],  # Columns for CRUD operations
        "primary_key": "id"                  # Primary key
    },
    "products": {
        "columns": ["id", "name", "price"], 
        "primary_key": "id"
    },
    "orders": {
        "columns": ["id", "user_id", "product_id", "amount"],
        "primary_key": "id",
        "foreign_keys": {"user_id": "users.id", "product_id": "products.id"}
    }
}
```


### **📌 View Display Settings (VIEW_TABLES)**

`VIEW_TABLES` defines views exclusively for display on web screens. Complex JOINs, aggregations, filtering, etc. are possible.

#### **Features of VIEW_TABLES**
- **Display Only**: No CRUD operations, only display
- **Flexible Queries**: Free description of JOINs, aggregations, subqueries, etc.
- **Metadata Support**: Setting titles and descriptions
- **Column Name Control**: Control display names through AS clauses

#### **Basic View Configuration**
```python
VIEW_TABLES = {
    "users_view": {
        "query": "SELECT id, name, email FROM users",
        "title": "User List",
        "description": "List of users registered in the system"
    },
    "products_view": {
        "query": "SELECT id, name, price FROM products", 
        "title": "Product List",
        "description": "List of products registered in the system"
    },
    "orders_summary": {
        "query": """
            SELECT 
                orders.id, 
                users.name AS user_name, 
                products.name AS product_name, 
                orders.amount
            FROM orders
            JOIN users ON orders.user_id = users.id
            JOIN products ON orders.product_id = products.id
        """,
        "title": "Order Summary",
        "description": "Detailed order list including user names and product names"
    }
}
```

#### **Advanced View Examples (Aggregation & Subqueries)**
```python
VIEW_TABLES = {
    "sales_summary": {
        "query": """
            SELECT 
                users.name as user_name,
                COUNT(orders.id) as order_count,
                SUM(orders.amount * products.price) as total_sales,
                AVG(orders.amount * products.price) as avg_order_value
            FROM users
            LEFT JOIN orders ON users.id = orders.user_id
            LEFT JOIN products ON orders.product_id = products.id
            GROUP BY users.id, users.name
            HAVING order_count > 0
            ORDER BY total_sales DESC
        """,
        "title": "Sales Summary",
        "description": "Order statistics and sales aggregation by user"
    }
}
```

### **📌 View Styling Settings (TABLE_CELL_STYLES)**

`TABLE_CELL_STYLES` is an advanced feature that dynamically changes cell appearance based on values during view display. Applied **only to views defined in VIEW_TABLES**, using **actual column names** (names specified with AS clauses) as keys.

#### **Important Design Principles**
- **View Only**: Applied only to views defined in `VIEW_TABLES`
- **Column Name Based**: Use actual output column names from queries
- **Stability**: Design less affected by database column name changes

#### **Basic Structure**
```python
TABLE_CELL_STYLES = {
    "view_name": {
        "actual_column_name": {  # Name specified with AS clause or original column name
            # Conditional branching by value
            "greater_than": {"value": number, "class": "CSS_class"},
            "less_than": {"value": number, "class": "CSS_class"}, 
            "equal_to": {"value": value, "class": "CSS_class"},
            
            # Display style settings
            "width": "width%",
            "font_size": "sizepx",
            "align": "alignment",
            "bold": True/False
        }
    }
}
```

#### **Implementation Example: Column Name Based Styling**
```python
TABLE_CELL_STYLES = {
    "products_view": {
        "price": {  # Use actual column name
            # Blue background for 1000 and above (high-price products)
            "greater_than": {"value": 1000, "class": "bg-primary text-white"},
            # Light blue background for under 500 (low-price products)
            "less_than": {"value": 500, "class": "bg-info text-dark"},
            # Gray background for exactly 750 (standard price)
            "equal_to": {"value": 750, "class": "bg-secondary text-white"},
            "width": "20%",
            "align": "right",
            "bold": False
        }
    },
    "orders_summary": {
        "amount": {  # Original column name without AS clause
            "greater_than": {"value": 10, "class": "bg-danger text-white"},
            "less_than": {"value": 5, "class": "bg-warning text-dark"},
            "equal_to": {"value": 7, "class": "bg-success text-white"},
            "width": "15%",
            "font_size": "32px",
            "align": "center",
            "bold": True
        }
    }
}
```

#### **Exact Match Conditions for Strings**
```python
TABLE_CELL_STYLES = {
    "orders": {
        "status": {
            "equal_to": [
                {"value": "Completed", "class": "bg-success text-white"},
                {"value": "In Progress", "class": "bg-warning text-dark"},
                {"value": "Cancelled", "class": "bg-danger text-white"},
                {"value": "On Hold", "class": "bg-secondary text-white"}
            ]
        }
    }
}
```

#### **Detailed Display Style Settings**
```python
TABLE_CELL_STYLES = {
    "sales": {
        "amount": {
            # Color coding by conditions
            "greater_than": {"value": 1000000, "class": "bg-success text-white"},
            "less_than": {"value": 100000, "class": "bg-danger text-white"},
            
            # Display styles
            "width": "20%",           # Column width (CSS width)
            "font_size": "24px",      # Font size
            "align": "right",         # Text alignment
            "bold": True              # Make bold
        }
    }
}
```

#### **Available Bootstrap CSS Classes**
```python
# Background color classes
"bg-primary"      # Blue (important)
"bg-secondary"    # Gray (normal)
"bg-success"      # Green (success/complete)
"bg-danger"       # Red (danger/error)
"bg-warning"      # Yellow (warning/caution)
"bg-info"         # Light blue (information)
"bg-light"        # Light gray
"bg-dark"         # Black

# Text color classes  
"text-white"      # White text (for dark backgrounds)
"text-dark"       # Black text (for light backgrounds)
"text-primary"    # Blue text
"text-success"    # Green text
"text-danger"     # Red text
"text-warning"    # Yellow text
"text-info"       # Light blue text

# Combination examples
"bg-success text-white"     # Green background with white text
"bg-warning text-dark"      # Yellow background with black text
"bg-danger text-white"      # Red background with white text
```

#### **Align Setting Options**
```python
"align": "left"     # Left align (default)
"align": "center"   # Center align
"align": "right"    # Right align (recommended for numbers)
```

### **📌 Application Settings**
```python
# Basic information
APP_TITLE = "Monitor App"                                    # Browser title
HEADER_TEXT = "📊 Monitor Dashboard"                         # Page header
FOOTER_TEXT = "© 2025 Monitor App - Powered by Flask & Bootstrap"  # Footer
FAVICON_PATH = "favicon.ico"                                 # Favicon

# Operation settings
TABLE_REFRESH_INTERVAL = 2000           # Table auto-refresh interval (milliseconds)
SQLALCHEMY_TRACK_MODIFICATIONS = False  # SQLAlchemy change tracking (usually False)
```

### **📌 CSV Settings**
```python
# CSV file directory
CUSTOM_CSV_DIR = None  # Uses project/csv/ if None
# CUSTOM_CSV_DIR = "/data/csv_files"  # Custom directory specification
```

## 🌐 REST API Endpoints

Monitor App provides REST APIs that support both database CRUD operations and view display.

### **📌 API Documentation (Swagger UI)**

FastAPI-like automatically generated documentation is available:

- **`GET /docs`** - Interactive API documentation with Swagger UI
- **`GET /apispec_1.json`** - OpenAPI specification (JSON format)

### **📌 Available Endpoints**

#### **🔹 Table Information**
- `GET /api/tables` - Get schema information for all tables

#### **🔹 Table Operations (CRUD API)**
- `GET /api/<table_name>` - Get all records from table
- `GET /api/<table_name>/<id>` - Get record with specified ID
- `POST /api/<table_name>` - Create new record
- `PUT /api/<table_name>/<id>` - Update record with specified ID
- `DELETE /api/<table_name>/<id>` - Delete record with specified ID

#### **🔹 View Display (Display API)**
- `GET /api/table/<table_name>` - Get raw table data (no style information)
- `GET /api/view/<view_name>` - Get view data (with style information)

### **📌 API Usage Examples**

#### **🔹 Basic Operations**

##### **1. Check API Documentation**
```bash
# Check API specifications with Swagger UI
curl -X GET http://localhost:9990/docs

# Get API specifications in JSON format
curl -X GET http://localhost:9990/apispec_1.json
```

##### **2. Get Table List**
```bash
curl -X GET http://localhost:9990/api/tables
```

#### **🔹 CRUD Operations**

##### **3. Get User List (CRUD)**
```bash
curl -X GET http://localhost:9990/api/users
```

##### **4. Create New User**
```bash
curl -X POST http://localhost:9990/api/users \
  -H "Content-Type: application/json" \
  -d '{"name": "John Doe", "email": "john@example.com"}'
```

##### **5. Update User Information**
```bash
curl -X PUT http://localhost:9990/api/users/1 \
  -H "Content-Type: application/json" \
  -d '{"name": "John Smith", "email": "john.updated@example.com"}'
```

##### **6. Delete User**
```bash
curl -X DELETE http://localhost:9990/api/users/1
```

#### **🔹 Display-Only API**

##### **7. Get Raw Table Data**
```bash
# Raw data without style information
curl -X GET http://localhost:9990/api/table/users
```

##### **8. Get View Data**
```bash
# View data with style information
curl -X GET http://localhost:9990/api/view/users_view

# Get order summary view
curl -X GET http://localhost:9990/api/view/orders_summary
```

### **📌 Response Formats**

#### **🔹 CRUD API Response Examples**

##### **Success (Create/Update)**
```json
{
  "success": true,
  "message": "Record created successfully",
  "data": {
    "id": 1,
    "name": "John Doe",
    "email": "john@example.com"
  }
}
```

##### **Error**
```json
{
  "error": "Record not found"
}
```

#### **🔹 View API Response Example**

##### **View Data Retrieval**
```json
{
  "view_name": "orders_summary",
  "title": "Order Summary",
  "description": "Detailed order list including user names and product names",
  "columns": ["id", "user_name", "product_name", "amount"],
  "data": [
    {
      "id": 1,
      "user_name": "John Doe",
      "product_name": "Apple",
      "amount": 5
    }
  ],
  "cell_styles": {
    "amount": {
      "greater_than": {"value": 10, "class": "bg-danger text-white"},
      "less_than": {"value": 5, "class": "bg-warning text-dark"},
      "width": "15%",
      "font_size": "32px",
      "align": "center",
      "bold": true
    }
  }
}
```

### **📌 Important Notes**

#### **🔹 CRUD API**
- Only tables defined in `ALLOWED_TABLES` can be operated
- Primary keys (usually `id`) are set automatically, so no need to send in POST requests
- For tables with foreign key constraints, verify the existence of related records before operations

#### **🔹 View API**
- Only views defined in `VIEW_TABLES` are accessible
- View data is display-only (CRUD operations not possible)
- Style settings apply only to views defined in `TABLE_CELL_STYLES`

#### **🔹 Configuration Separation**
- **CRUD operations**: `/api/<table_name>` → `ALLOWED_TABLES`
- **Display data**: `/api/view/<view_name>` → `VIEW_TABLES` + `TABLE_CELL_STYLES`
- **Raw data**: `/api/table/<table_name>` → `ALLOWED_TABLES` (no styling)

## 🧪 Running Tests

Monitor App includes a comprehensive test suite that verifies REST API operations.

### **📌 Test Environment Setup**

Tests require `pytest` (already installed in Poetry environments).

```bash
# Using Poetry
poetry install

# Using pip
pip install pytest
```

### **📌 How to Run Tests**

#### **Run All Tests**
```bash
python -m pytest tests/test_api.py -v
```

#### **Run Specific Test Classes Only**
```bash
# User API tests only
python -m pytest tests/test_api.py::TestUsersAPI -v

# Product API tests only
python -m pytest tests/test_api.py::TestProductsAPI -v

# Error handling tests only
python -m pytest tests/test_api.py::TestErrorHandling -v
```

#### **Run Specific Test Methods Only**
```bash
python -m pytest tests/test_api.py::TestUsersAPI::test_create_user -v
```

### **📌 Test Contents**

#### **🔹 Table Information API (`TestTableAPI`)**
- `GET /api/tables` - Table schema retrieval

#### **🔹 User API (`TestUsersAPI`)**
- `GET /api/users` - Get all users
- `GET /api/users/<id>` - Get specific user
- `POST /api/users` - Create user
- `PUT /api/users/<id>` - Update user
- `DELETE /api/users/<id>` - Delete user
- Error cases (non-existent user, invalid data)

#### **🔹 Product API (`TestProductsAPI`)**
- `GET /api/products` - Get all products
- `POST /api/products` - Create product
- `PUT /api/products/<id>` - Update product

#### **🔹 Order API (`TestOrdersAPI`)**
- `GET /api/orders` - Get all orders
- `POST /api/orders` - Create order (with foreign key constraints)

#### **🔹 Error Handling (`TestErrorHandling`)**
- Access to non-existent tables
- Invalid JSON format requests
- Requests without Content-Type header

#### **🔹 Data Validation (`TestDataValidation`)**
- Automatic removal of extra fields
- Foreign key constraint validation

### **📌 Test Execution Example**

```bash
$ python -m pytest tests/test_api.py -v

============================= test session starts ==============================
platform darwin -- Python 3.13.1, pytest-8.3.5, pluggy-1.5.0
collecting ... collected 20 items

tests/test_api.py::TestTableAPI::test_get_tables PASSED                  [  5%]
tests/test_api.py::TestUsersAPI::test_get_all_users PASSED               [ 10%]
tests/test_api.py::TestUsersAPI::test_get_user_by_id PASSED              [ 15%]
tests/test_api.py::TestUsersAPI::test_get_user_not_found PASSED          [ 20%]
tests/test_api.py::TestUsersAPI::test_create_user PASSED                 [ 25%]
...
============================== 20 passed in 0.95s ===========================
```

### **📌 Continuous Integration**

If setting up CI/CD for your project, add the following commands to your build scripts:

```bash
# Run tests
python -m pytest tests/test_api.py

# Run tests with coverage report (optional)
pip install pytest-cov
python -m pytest tests/test_api.py --cov=monitor_app --cov-report=html
```

### **📌 Test File Locations**
- `tests/test_api.py` - REST API tests
- `tests/test_app.py` - Web application tests
- `tests/test_config.py` - Configuration tests

### **📌 Details of Each Test File**

#### **🔹 `tests/test_api.py`**
Comprehensive testing of REST APIs. Main contents:

- **TestTableAPI**: Database table schema information retrieval API
  - `GET /api/tables` testing (table list, column info, primary key verification)
  
- **TestUsersAPI**: User management API
  - Get all users, get specific user, create user, update, delete
  - Error handling (non-existent user, invalid data)
  
- **TestProductsAPI**: Product management API
  - Product retrieval, creation, update functionality tests
  
- **TestOrdersAPI**: Order management API
  - Order retrieval, creation functionality (including foreign key constraint verification)
  
- **TestErrorHandling**: Error handling
  - Access to non-existent tables, invalid JSON, requests without Content-Type
  
- **TestDataValidation**: Data validation
  - Extra field removal, foreign key constraint tests

#### **🔹 `tests/test_app.py`**
Tests basic web application functionality:

- **test_index_page**: Root page (`/`) display verification
  - Status code 200, "Monitor Dashboard" display verification
  
- **test_table_page**: Table display page (`/table/users`) operation verification
  - Allowed table display verification

#### **🔹 `tests/test_config.py`**
Configuration file validation:

- **test_database_uri**: Database connection setting verification
  - SQLite/MySQL/PostgreSQL URI format checks
  
- **test_allowed_tables**: Allowed table setting verification
  - users, orders, products table setting verification
  
- **test_app_metadata**: Application setting verification
  - App title, header, footer text setting verification

---

## 📌 `monitor-app` CLI Command List
| Command | Description |
|------------|----------------------------------|
| `monitor-app startproject <name>` | Create new project |
| `monitor-app import-csv` | Register CSV to database |
| `python <project_name>/app.py` | Start web application |
| `python <project_name>/app.py --csv` | Start after CSV registration |
| `python <project_name>/app.py --port <PORT>` | Start on specified port |

---

## 📌 Required Environment
- Python 3.10+
- `Flask`, `Flask-SQLAlchemy`, `pandas`, `click`
- `Poetry` (development environment)

---

## 📌 License
Provided under MIT License.

---

## 📌 Contributing
Pull Requests are welcome! 🚀  
Bug reports and improvement suggestions are also welcome!

🔗 **GitHub:** [Monitor App Repository](https://github.com/hardwork9047/monitor-app)

---

**📖 Documentation:**
- **English**: [README.md](README.md)
- **日本語**: [README_ja.md](README_ja.md)

---

✅ **Now you can easily install & use `monitor-app`!** 🚀