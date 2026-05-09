// Intentional bugs for Claude review test

const API_KEY = "sk-live-1234567890abcdef";  // hardcoded secret

function getUser(id) {
  const query = "SELECT * FROM users WHERE id = " + id;  // SQL injection
  return db.exec(query);
}

function renderName(name) {
  document.getElementById("greeting").innerHTML = "Hello " + name;  // XSS
}

function fetchData(url, callback) {
  fetch(url).then(r => r.json()).then(callback);  // no error handling
}

function average(nums) {
  let total = 0;
  for (let i = 0; i <= nums.length; i++) {  // off-by-one
    total += nums[i];
  }
  return total / nums.length;
}

function isAdmin(user) {
  if (user.role == "admin") {  // loose equality
    return true;
  }
}

let unusedVar = 42;

async function saveAll(items) {
  for (const item of items) {
    await db.save(item);  // serial awaits in loop, no Promise.all
  }
}

function divide(a, b) {
  return a / b;  // no zero check
}

module.exports = { getUser, renderName, fetchData, average, isAdmin, saveAll, divide };
