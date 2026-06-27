import json
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("inventory-server")

@mcp.tool()
def get_inventory_levels() -> str:
    """Get current inventory levels including product ID, name, current stock, and threshold."""
    inventory = [
        {"product_id": "P101", "name": "Wireless Mouse", "stock": 15, "threshold": 20, "price": 25.00},
        {"product_id": "P102", "name": "Mechanical Keyboard", "stock": 8, "threshold": 10, "price": 75.00},
        {"product_id": "P103", "name": "USB-C Hub", "stock": 45, "threshold": 15, "price": 40.00},
        {"product_id": "P104", "name": "Ergonomic Chair", "stock": 3, "threshold": 5, "price": 250.00},
        {"product_id": "P105", "name": "LED Monitor", "stock": 12, "threshold": 8, "price": 180.00}
    ]
    return json.dumps(inventory)

@mcp.tool()
def get_sales_history() -> str:
    """Get the historical weekly sales units for products."""
    sales = [
        {"product_id": "P101", "weekly_sales": [12, 14, 18, 15]},
        {"product_id": "P102", "weekly_sales": [4, 5, 3, 6]},
        {"product_id": "P103", "weekly_sales": [20, 25, 22, 28]},
        {"product_id": "P104", "weekly_sales": [2, 1, 2, 3]},
        {"product_id": "P105", "weekly_sales": [5, 4, 6, 5]}
    ]
    return json.dumps(sales)

@mcp.tool()
def get_supplier_details(product_id: str) -> str:
    """Get the supplier name, order contact email, and lead time in days for a specific product ID."""
    suppliers = {
        "P101": {"supplier": "LogiTech Wholesale", "email": "sales@logitechwholesale.com", "lead_time_days": 5},
        "P102": {"supplier": "Keyboards & Co", "email": "orders@keyboardsandco.com", "lead_time_days": 7},
        "P103": {"supplier": "HubConnect Inc", "email": "support@hubconnect.com", "lead_time_days": 4},
        "P104": {"supplier": "Comfort Seating Ltd", "email": "sales@comfortseating.com", "lead_time_days": 10},
        "P105": {"supplier": "DisplayTech Corp", "email": "contact@displaytech.com", "lead_time_days": 6}
    }
    info = suppliers.get(product_id, {"supplier": "Generic Supplier Ltd", "email": "info@genericsupplier.com", "lead_time_days": 5})
    return json.dumps(info)

@mcp.tool()
def save_purchase_order(po_json: str) -> str:
    """Save/commit a drafted purchase order.
    
    Args:
        po_json: A JSON string containing purchase order details.
    """
    try:
        data = json.loads(po_json)
        po_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "artifacts"))
        os.makedirs(po_dir, exist_ok=True)
        po_path = os.path.join(po_dir, f"po_{data.get('po_number', 'unknown')}.json")
        with open(po_path, "w") as f:
            json.dump(data, f, indent=2)
        return json.dumps({"status": "success", "saved_path": po_path})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

if __name__ == "__main__":
    mcp.run()
