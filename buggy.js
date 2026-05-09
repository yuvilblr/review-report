// Intentional bugs for Claude review test (fixed)

const API_KEY = process.env.API_KEY;

function getUser(id) {
  return db.exec("SELECT * FROM users WHERE id = ?", [id]);
}

function renderName(name) {
  const el = document.getElementById("greeting");
  el.textContent = "Hello " + name;
}

function fetchData(url) {
  return fetch(url).then((r) => {
    if (!r.ok) {
      throw new Error("Request failed: " + r.status);
    }
    return r.json();
  });
}

function average(nums) {
  if (nums.length === 0) return 0;
  let total = 0;
  for (let i = 0; i < nums.length; i++) {
    total += nums[i];
  }
  return total / nums.length;
}

function isAdmin(user) {
  return user.role === "admin";
}

async function saveAll(items) {
  await Promise.all(items.map((item) => db.save(item)));
}

function divide(a, b) {
  if (b === 0) {
    throw new Error("Division by zero");
  }
  return a / b;
}

module.exports = { getUser, renderName, fetchData, average, isAdmin, saveAll, divide };
