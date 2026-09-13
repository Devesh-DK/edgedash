import React, { useState, useRef } from 'react';
import { StyleSheet, View, Text, ActivityIndicator, Animated, SafeAreaView, StatusBar, Platform } from 'react-native';
import { WebView } from 'react-native-webview';

// Using the provided placeholder URL for the deployed Streamlit app
const STREAMLIT_URL = 'https://edgedash.streamlit.app';

export default function App() {
  const [isLoading, setIsLoading] = useState(true);
  const fadeAnim = useRef(new Animated.Value(1)).current;

  // Function to smoothly fade out the splash screen once WebView loads
  const handleLoadEnd = () => {
    Animated.timing(fadeAnim, {
      toValue: 0,
      duration: 500, // half a second fade out
      useNativeDriver: true,
    }).start(() => {
      setIsLoading(false);
    });
  };

  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="dark-content" backgroundColor="#f8f9fa" />
      
      <WebView
        source={{ uri: STREAMLIT_URL }}
        style={styles.webview}
        onLoadEnd={handleLoadEnd}
        // These settings optimize the webview behavior
        javaScriptEnabled={true}
        domStorageEnabled={true}
        startInLoadingState={false} // We handle our own loading state
      />

      {/* Custom Gemini-style Splash Screen overlay */}
      {isLoading && (
        <Animated.View style={[styles.splashScreen, { opacity: fadeAnim }]} pointerEvents="none">
          <View style={styles.splashContent}>
            {/* Soft, pulsating loading indicator */}
            <ActivityIndicator size="large" color="#1a73e8" />
            
            <Text style={styles.splashTitle}>EdgeDash</Text>
            <Text style={styles.splashSubtitle}>Starting intelligence agent...</Text>
          </View>
        </Animated.View>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#f8f9fa', // Gemini light background
    // Extra padding for Android notch if needed, but SafeAreaView handles iOS
    paddingTop: Platform.OS === 'android' ? StatusBar.currentHeight : 0,
  },
  webview: {
    flex: 1,
    backgroundColor: '#f8f9fa',
  },
  splashScreen: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: '#ffffff',
    justifyContent: 'center',
    alignItems: 'center',
    zIndex: 10,
  },
  splashContent: {
    alignItems: 'center',
    padding: 40,
    backgroundColor: '#ffffff',
    borderRadius: 24,
    shadowColor: '#1a73e8',
    shadowOffset: { width: 0, height: 10 },
    shadowOpacity: 0.05,
    shadowRadius: 30,
    elevation: 3,
  },
  splashTitle: {
    marginTop: 20,
    fontSize: 28,
    fontWeight: '800',
    color: '#1e293b',
    letterSpacing: -0.5,
  },
  splashSubtitle: {
    marginTop: 8,
    fontSize: 14,
    color: '#64748b',
    fontWeight: '500',
  },
});
