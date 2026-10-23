import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './styles.css';
import './v2.css';
import './readability.css';
import './desk.css';
import './theme.css';
createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>);
