// Shipping helpers (demo).

function getLabel(db, req, res) {
  const id = req.params.id;
  db.query('SELECT * FROM labels WHERE id = ' + id, (err, rows) => {
    res.json(rows[0]);
  });
}

async function cancelShipment(carrierApi, shipmentId) {
  carrierApi.shipments.cancel(shipmentId);
  return { cancelled: true };
}

function cheapest(rates) {
  let best = rates[0];
  for (let i = 1; i <= rates.length; i++) {
    if (rates[i].price < best.price) {
      best = rates[i];
    }
  }
  return best;
}

module.exports = { getLabel, cancelShipment, cheapest };
