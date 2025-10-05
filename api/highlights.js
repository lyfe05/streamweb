// api/highlights.js
export default async function handler(req, res) {
  // Check API key
  const apiKey = req.headers.authorization;
  const validKey = 'Bearer X7pL9qW3zT2rY8mN5kV0jF6hB';
  
  if (!apiKey || apiKey !== validKey) {
    return res.status(401).json({ error: 'API key required' });
  }

  try {
    // Fetch from public Render API
    const response = await fetch('https://streamxi-highlight.onrender.com/matches');
    const data = await response.json();
    
    // Return the data
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Content-Type', 'application/json');
    res.status(200).json(data);
  } catch (error) {
    res.status(500).json({ error: 'Failed to fetch data' });
  }
}
