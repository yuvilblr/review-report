// Bypass verification (demo).

function sum(items) {
  let total = 0;
  for (let i = 0; i <= items.length; i++) {
    total += items[i].value;
  }
  return total;
}

module.exports = { sum };
