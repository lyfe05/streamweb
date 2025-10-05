// middleware.js
import { NextResponse } from 'next/server'

export function middleware(request) {
  const apiKey = request.headers.get('authorization')
  const validKey = 'Bearer X7pL9qW3zT2rY8mN5kV0jF6hB'

  // Check if the request is for highlights.json
  if (request.nextUrl.pathname === '/highlights.json') {
    // Verify API key
    if (apiKey !== validKey) {
      return new Response(JSON.stringify({ error: 'API key required' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' }
      })
    }
    
    // Add API key to the rewrite
    const response = NextResponse.next()
    response.headers.set('Authorization', 'Bearer X7pL9qW3zT2rY8mN5kV0jF6hB')
    return response
  }
}

export const config = {
  matcher: '/highlights.json'
}
