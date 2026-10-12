// Copyright (C) 2026 Analog Devices, Inc.
//
// SPDX short identifier: ADIBSD
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:admt4000_dashboard/main.dart';

void main() {
  testWidgets('App launches to the connection screen', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(const Admt4000DashboardApp());
    // ConnectionScreen shows a CircularProgressIndicator (an indeterminate,
    // continuously-animating spinner) while SharedPreferences loads, so
    // pumpAndSettle() would hang forever waiting for it to stop animating.
    // Pump a few frames instead to let that async load complete.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));

    expect(find.text('ADMT4000 Dashboard'), findsOneWidget);
    expect(find.text('Connect'), findsOneWidget);
  });
}
