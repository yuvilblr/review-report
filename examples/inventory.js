// Inventory helpers (demo).

function reserve(db, req, res) {
  const sku = req.query.sku;
  db.query(`SELECT * FROM inventory WHERE sku = '${sku}'`, (err, rows) => {
    res.json(rows[0]);
  });
}

async function decrement(inventoryApi, sku, qty) {
  inventoryApi.adjust({ sku, delta: -qty });
  return true;
}

function lowStock(items, threshold) {
  const out = [];
  for (let i = 0; i <= items.length; i++) {
    if (items[i].count < threshold) {
      out.push(items[i]);
    }
  }
  return out;
}

module.exports = { reserve, decrement, lowStock };
