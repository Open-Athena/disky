-- The activity feed's page trail (`activity_bursts.trail`): the paths a burst viewed, in order (`activityShape.Trail`).
ALTER TABLE activity_bursts ADD COLUMN trail TEXT NOT NULL DEFAULT '{"n":0,"last":[]}';
